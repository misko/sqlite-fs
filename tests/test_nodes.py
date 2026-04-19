import pytest

from sqlite_fs import Filesystem
from sqlite_fs.errors import (
    AlreadyExists,
    DirectoryNotEmpty,
    IsADirectory,
    NotADirectory,
    NotFound,
    PermissionDenied,
)


def test_mkdir_creates_dir(as_root):
    as_root.mkdir("/a")
    entries = as_root.readdir("/")
    assert [e.name for e in entries] == ["a"]
    assert entries[0].kind == "dir"


def test_mkdir_on_existing_raises(as_root):
    as_root.mkdir("/a")
    with pytest.raises(AlreadyExists):
        as_root.mkdir("/a")


def test_mkdir_missing_parent_raises(as_root):
    with pytest.raises(NotFound):
        as_root.mkdir("/no/such/parent/child")


def test_mkdir_records_mode(as_root):
    as_root.mkdir("/a", mode=0o750)
    st = as_root.stat("/a")
    assert st.mode & 0o777 == 0o750


def test_mkdir_records_kind_dir(as_root):
    as_root.mkdir("/a")
    st = as_root.stat("/a")
    assert st.kind == "dir"


def test_rmdir_removes_empty(as_root):
    as_root.mkdir("/a")
    as_root.rmdir("/a")
    assert as_root.readdir("/") == []
    assert as_root.exists("/a") is False


def test_rmdir_non_empty_raises(as_root):
    as_root.mkdir("/a")
    as_root.mkdir("/a/b")
    with pytest.raises(DirectoryNotEmpty):
        as_root.rmdir("/a")


def test_rmdir_on_file_raises_not_a_directory(as_root):
    fd = as_root.create("/f")
    as_root.close_fd(fd)
    with pytest.raises(NotADirectory):
        as_root.rmdir("/f")


def test_rmdir_on_root_raises(as_root):
    with pytest.raises(PermissionDenied):
        as_root.rmdir("/")


def test_rmdir_nonexistent_raises(as_root):
    with pytest.raises(NotFound):
        as_root.rmdir("/no-such-dir")


def test_readdir_empty(fresh_fs):
    assert fresh_fs.readdir("/") == []


def test_readdir_excludes_dot_and_dotdot(as_root):
    as_root.mkdir("/a")
    as_root.mkdir("/b")
    names = sorted(e.name for e in as_root.readdir("/"))
    assert names == ["a", "b"]


def test_readdir_stable_across_reopen(tmp_db):
    from sqlite_fs import mkfs, open_fs

    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fs.mkdir("/a")
        fs.mkdir("/b")
        fs.mkdir("/c")
        names_before = sorted(e.name for e in fs.readdir("/"))
    fs.close()

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        names_after = sorted(e.name for e in fs.readdir("/"))
    fs.close()

    assert names_after == names_before == ["a", "b", "c"]


def test_readdir_on_file_raises(as_root):
    fd = as_root.create("/f")
    as_root.close_fd(fd)
    with pytest.raises(NotADirectory):
        as_root.readdir("/f")


def test_stat_root(fresh_fs):
    st = fresh_fs.stat("/")
    assert st.kind == "dir"
    assert st.inode == 1
    assert st.nlink == 2


def test_stat_file_size_and_nlink(as_root):
    fd = as_root.create("/f")
    as_root.write(fd, b"hello", offset=0)
    as_root.close_fd(fd)

    st = as_root.stat("/f")
    assert st.kind == "file"
    assert st.size == 5
    assert st.nlink == 1


def test_stat_nonexistent_raises(fresh_fs):
    with pytest.raises(NotFound):
        fresh_fs.stat("/no-such-file")


def test_parent_mtime_updates_on_child_create(as_root):
    mtime_before = as_root.stat("/").mtime_ns
    import time; time.sleep(0.001)
    as_root.mkdir("/a")
    mtime_after = as_root.stat("/").mtime_ns
    assert mtime_after > mtime_before


def test_dir_nlink_counts_subdirs(as_root):
    as_root.mkdir("/a")
    assert as_root.stat("/a").nlink == 2

    as_root.mkdir("/a/sub1")
    as_root.mkdir("/a/sub2")
    assert as_root.stat("/a").nlink == 4

    fd = as_root.create("/a/file")
    as_root.close_fd(fd)
    assert as_root.stat("/a").nlink == 4

    as_root.rmdir("/a/sub1")
    assert as_root.stat("/a").nlink == 3


def test_unique_parent_name_constraint(as_root):
    fd = as_root.create("/x")
    as_root.close_fd(fd)
    with pytest.raises(AlreadyExists):
        as_root.mkdir("/x")


def test_inodes_are_unique_and_stable(as_root):
    as_root.mkdir("/a")
    fd = as_root.create("/b")
    as_root.close_fd(fd)

    inode_a = as_root.stat("/a").inode
    inode_b = as_root.stat("/b").inode
    inode_root = as_root.stat("/").inode

    assert inode_root == 1
    assert inode_a != inode_b != inode_root
    assert inode_a > 1 and inode_b > 1
