import pytest
import sqlite3

from sqlite_fs import mkfs, open_fs, Filesystem
from sqlite_fs.errors import (
    AlreadyExists,
    FilesystemError,
    PermissionDenied,
    ReadOnlyFilesystem,
)


def test_mkfs_creates_integrity_ok_db(tmp_db):
    mkfs(str(tmp_db))
    assert tmp_db.exists()

    conn = sqlite3.connect(str(tmp_db))
    result = conn.execute("PRAGMA integrity_check").fetchone()
    conn.close()
    assert result == ("ok",)


def test_mkfs_refuses_overwrite_by_default(tmp_db):
    mkfs(str(tmp_db))
    with pytest.raises(AlreadyExists):
        mkfs(str(tmp_db))


def test_mkfs_overwrite_truncates(tmp_db):
    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fs.mkdir("/existing")
    fs.close()

    mkfs(str(tmp_db), overwrite=True)
    fs = open_fs(str(tmp_db))
    assert fs.readdir("/") == []
    fs.close()


def test_open_fs_context_manager(tmp_db):
    mkfs(str(tmp_db))
    with open_fs(str(tmp_db)) as fs:
        with fs.as_user(0, 0):
            fs.mkdir("/a")


def test_mkfs_creates_root_at_inode_1(fresh_fs):
    st = fresh_fs.stat("/")
    assert st.inode == 1
    assert st.kind == "dir"


def test_as_user_switches_effective_identity(fresh_fs):
    with fresh_fs.as_user(0, 0):
        fresh_fs.mkdir("/a", mode=0o700)

    with fresh_fs.as_user(1000, 1000):
        with pytest.raises(PermissionDenied):
            fresh_fs.readdir("/a")

    with fresh_fs.as_user(0, 0):
        assert fresh_fs.readdir("/a") == []


def test_as_user_restores_on_exception(fresh_fs):
    with fresh_fs.as_user(0, 0):
        fresh_fs.mkdir("/a")

    try:
        with fresh_fs.as_user(1000, 1000):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with fresh_fs.as_user(0, 0):
        fresh_fs.mkdir("/b")


def test_readonly_mount_refuses_mutation(tmp_db):
    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fs.mkdir("/seed")
    fs.close()

    fs = open_fs(str(tmp_db), readonly=True)
    _ = fs.readdir("/")   # readable
    with fs.as_user(0, 0):
        with pytest.raises(ReadOnlyFilesystem):
            fs.mkdir("/new")
        with pytest.raises(ReadOnlyFilesystem):
            fs.unlink("/seed")
    fs.close()


def test_fsck_clean_on_fresh(fresh_fs):
    report = fresh_fs.fsck()
    assert report.issues == []
    assert report.integrity_check_result == "ok"


def test_fsck_detects_orphan_blob(tmp_db):
    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fd = fs.create("/a")
        fs.write(fd, b"x" * 1024, offset=0)
        fs.close_fd(fd)
    fs.close()

    conn = sqlite3.connect(str(tmp_db))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO blobs (inode, chunk_id, data) VALUES (9999, 0, x'41')")
    conn.commit()
    conn.close()

    fs = open_fs(str(tmp_db))
    report = fs.fsck()
    fs.close()
    assert any(issue.kind == "orphan_blob" and issue.inode == 9999
               for issue in report.issues)


def test_filesystem_is_a_context_manager(fresh_fs):
    assert hasattr(Filesystem, "__enter__")
    assert hasattr(Filesystem, "__exit__")


def test_reopen_preserves_all_state(tmp_db):
    mkfs(str(tmp_db))

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fs.mkdir("/dir", mode=0o755)
        fd = fs.create("/dir/file", mode=0o644)
        fs.write(fd, b"content bytes", offset=0)
        fs.close_fd(fd)
        fs.symlink(b"/dir/file", "/link")
        fs.setxattr("/dir/file", "user.tag", b"v1")
    fs.close()

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        assert sorted(e.name for e in fs.readdir("/")) == ["dir", "link"]
        assert [e.name for e in fs.readdir("/dir")] == ["file"]
        fd = fs.open("/dir/file", flags=0)
        assert fs.read(fd, size=1000, offset=0) == b"content bytes"
        fs.close_fd(fd)
        assert fs.readlink("/link") == b"/dir/file"
        assert fs.getxattr("/dir/file", "user.tag") == b"v1"
    fs.close()
