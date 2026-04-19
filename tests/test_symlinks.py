import pytest

from sqlite_fs.errors import (
    AlreadyExists,
    IsADirectory,
    NotFound,
    SymlinkLoop,
)


def test_symlink_create_and_readlink(as_root):
    as_root.symlink(b"/target/path", "/link")
    assert as_root.readlink("/link") == b"/target/path"


def test_symlink_target_stored_as_bytes(as_root):
    non_utf8_target = b"\xff\xfe\xfd/path"
    as_root.symlink(non_utf8_target, "/link")
    assert as_root.readlink("/link") == non_utf8_target


def test_symlink_target_need_not_exist(as_root):
    as_root.symlink(b"/no/such/file", "/dangling")
    assert as_root.readlink("/dangling") == b"/no/such/file"
    st = as_root.stat("/dangling", follow_symlinks=False)
    assert st.kind == "symlink"
    with pytest.raises(NotFound):
        as_root.stat("/dangling", follow_symlinks=True)


def test_stat_follow_symlink(as_root):
    fd = as_root.create("/target", mode=0o644)
    as_root.write(fd, b"hello", offset=0)
    as_root.close_fd(fd)

    as_root.symlink(b"/target", "/link")

    st_target = as_root.stat("/target")
    st_link_followed = as_root.stat("/link")
    assert st_link_followed.inode == st_target.inode
    assert st_link_followed.size == 5
    assert st_link_followed.kind == "file"


def test_lstat_does_not_follow(as_root):
    fd = as_root.create("/target"); as_root.close_fd(fd)
    as_root.symlink(b"/target", "/link")

    st_link = as_root.lstat("/link")
    st_target = as_root.stat("/target")

    assert st_link.kind == "symlink"
    assert st_link.inode != st_target.inode


def test_stat_follow_false_equals_lstat(as_root):
    fd = as_root.create("/target"); as_root.close_fd(fd)
    as_root.symlink(b"/target", "/link")

    a = as_root.stat("/link", follow_symlinks=False)
    b = as_root.lstat("/link")
    assert a == b


def test_readlink_on_non_symlink_raises(as_root):
    fd = as_root.create("/f"); as_root.close_fd(fd)
    with pytest.raises(NotFound):
        as_root.readlink("/f")

    as_root.mkdir("/d")
    with pytest.raises(NotFound):
        as_root.readlink("/d")


def test_symlink_on_existing_raises(as_root):
    fd = as_root.create("/existing"); as_root.close_fd(fd)
    with pytest.raises(AlreadyExists):
        as_root.symlink(b"/target", "/existing")


def test_unlink_symlink_removes_only_link(as_root):
    fd = as_root.create("/target")
    as_root.write(fd, b"data", offset=0)
    as_root.close_fd(fd)

    as_root.symlink(b"/target", "/link")
    as_root.unlink("/link")

    assert as_root.exists("/target")
    assert as_root.stat("/target").size == 4
    assert as_root.exists("/link") is False


def test_symlink_chain_follow(as_root):
    fd = as_root.create("/target"); as_root.close_fd(fd)
    target_inode = as_root.stat("/target").inode

    as_root.symlink(b"/target", "/link0")
    for i in range(1, 10):
        as_root.symlink(f"/link{i - 1}".encode(), f"/link{i}")

    assert as_root.stat("/link9").inode == target_inode


def test_symlink_chain_too_long_raises(as_root):
    as_root.symlink(b"/nonexistent", "/link0")
    for i in range(1, 42):
        as_root.symlink(f"/link{i - 1}".encode(), f"/link{i}")

    with pytest.raises(SymlinkLoop):
        as_root.stat("/link41", follow_symlinks=True)


def test_symlink_loop_raises(as_root):
    as_root.symlink(b"/a", "/a")
    with pytest.raises(SymlinkLoop):
        as_root.stat("/a", follow_symlinks=True)

    st = as_root.stat("/a", follow_symlinks=False)
    assert st.kind == "symlink"


def test_readdir_includes_symlink_kind(as_root):
    fd = as_root.create("/target"); as_root.close_fd(fd)
    as_root.symlink(b"/target", "/link")

    entries = {e.name: e.kind for e in as_root.readdir("/")}
    assert entries == {"target": "file", "link": "symlink"}


def test_symlinks_survive_reopen(tmp_db):
    from sqlite_fs import mkfs, open_fs

    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fs.symlink(b"/persisted/target", "/link")
    fs.close()

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        assert fs.readlink("/link") == b"/persisted/target"
    fs.close()
