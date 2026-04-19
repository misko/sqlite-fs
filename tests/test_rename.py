import pytest

from sqlite_fs.errors import (
    AlreadyExists,
    DirectoryNotEmpty,
    FilesystemError,
    NotFound,
    PermissionDenied,
)


def test_rename_same_parent_file(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"data", offset=0)
    as_root.close_fd(fd)

    inode_before = as_root.stat("/a").inode
    as_root.rename("/a", "/b")

    assert as_root.exists("/a") is False
    assert as_root.exists("/b")
    assert as_root.stat("/b").inode == inode_before
    fd = as_root.open("/b", flags=0)
    assert as_root.read(fd, size=4, offset=0) == b"data"
    as_root.close_fd(fd)


def test_rename_cross_parent_file(as_root):
    as_root.mkdir("/dir1")
    as_root.mkdir("/dir2")
    fd = as_root.create("/dir1/a")
    as_root.close_fd(fd)

    inode_before = as_root.stat("/dir1/a").inode
    as_root.rename("/dir1/a", "/dir2/a")

    assert as_root.exists("/dir1/a") is False
    assert as_root.stat("/dir2/a").inode == inode_before


def test_rename_overwrites_existing_file(as_root):
    fd = as_root.create("/a"); as_root.write(fd, b"source", offset=0); as_root.close_fd(fd)
    fd = as_root.create("/b"); as_root.write(fd, b"target", offset=0); as_root.close_fd(fd)

    inode_a = as_root.stat("/a").inode
    as_root.rename("/a", "/b")

    assert as_root.stat("/b").inode == inode_a
    fd = as_root.open("/b", flags=0)
    assert as_root.read(fd, size=6, offset=0) == b"source"
    as_root.close_fd(fd)
    assert as_root.exists("/a") is False


def test_rename_onto_nonempty_dir_raises(as_root):
    as_root.mkdir("/a")
    as_root.mkdir("/b")
    fd = as_root.create("/b/inner"); as_root.close_fd(fd)

    with pytest.raises(DirectoryNotEmpty):
        as_root.rename("/a", "/b")
    assert as_root.exists("/a")
    assert as_root.exists("/b/inner")


def test_rename_onto_empty_dir_succeeds(as_root):
    as_root.mkdir("/src", mode=0o700)
    as_root.mkdir("/dst", mode=0o755)

    as_root.rename("/src", "/dst")
    assert as_root.exists("/src") is False
    st = as_root.stat("/dst")
    assert st.mode & 0o777 == 0o700


def test_rename_into_own_subtree_raises(as_root):
    as_root.mkdir("/a")
    as_root.mkdir("/a/b")
    as_root.mkdir("/a/b/c")

    with pytest.raises(FilesystemError):
        as_root.rename("/a", "/a/b/c/a")

    with pytest.raises(FilesystemError):
        as_root.rename("/a", "/a/b/c")

    assert as_root.exists("/a/b/c")


def test_rename_src_equals_dst_is_noop(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    inode_before = as_root.stat("/a").inode
    mtime_before = as_root.stat("/a").mtime_ns

    as_root.rename("/a", "/a")

    assert as_root.stat("/a").inode == inode_before
    assert as_root.stat("/a").mtime_ns == mtime_before


def test_rename_nonexistent_src_raises(as_root):
    with pytest.raises(NotFound):
        as_root.rename("/no-such", "/b")


def test_rename_to_missing_parent_raises(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    with pytest.raises(NotFound):
        as_root.rename("/a", "/no/such/dir/a")


def test_rename_noreplace_on_existing_raises(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    fd = as_root.create("/b"); as_root.close_fd(fd)

    with pytest.raises(AlreadyExists):
        as_root.rename("/a", "/b", noreplace=True)
    assert as_root.exists("/a")
    assert as_root.exists("/b")


def test_rename_exchange_swaps_paths(as_root):
    fd = as_root.create("/a"); as_root.write(fd, b"aaa", offset=0); as_root.close_fd(fd)
    fd = as_root.create("/b"); as_root.write(fd, b"bbb", offset=0); as_root.close_fd(fd)

    inode_a = as_root.stat("/a").inode
    inode_b = as_root.stat("/b").inode

    as_root.rename("/a", "/b", exchange=True)

    assert as_root.stat("/a").inode == inode_b
    fd = as_root.open("/a", flags=0)
    assert as_root.read(fd, size=3, offset=0) == b"bbb"
    as_root.close_fd(fd)

    assert as_root.stat("/b").inode == inode_a
    fd = as_root.open("/b", flags=0)
    assert as_root.read(fd, size=3, offset=0) == b"aaa"
    as_root.close_fd(fd)


def test_rename_exchange_requires_both_exist(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    with pytest.raises(NotFound):
        as_root.rename("/a", "/no-such", exchange=True)


def test_rename_updates_parent_mtimes(as_root):
    import time
    as_root.mkdir("/src-parent")
    as_root.mkdir("/dst-parent")
    fd = as_root.create("/src-parent/f"); as_root.close_fd(fd)

    src_parent_mtime_before = as_root.stat("/src-parent").mtime_ns
    dst_parent_mtime_before = as_root.stat("/dst-parent").mtime_ns
    file_mtime_before = as_root.stat("/src-parent/f").mtime_ns

    time.sleep(0.001)
    as_root.rename("/src-parent/f", "/dst-parent/f")

    assert as_root.stat("/src-parent").mtime_ns > src_parent_mtime_before
    assert as_root.stat("/dst-parent").mtime_ns > dst_parent_mtime_before
    assert as_root.stat("/dst-parent/f").mtime_ns == file_mtime_before


def test_rename_preserves_symlink_target_not_resolved(as_root):
    as_root.symlink(b"/target/does/not/exist", "/link")
    as_root.rename("/link", "/relocated")
    assert as_root.readlink("/relocated") == b"/target/does/not/exist"


def test_rename_atomicity_hint(as_root):
    fd = as_root.create("/src"); as_root.write(fd, b"x", offset=0); as_root.close_fd(fd)

    as_root.rename("/src", "/dst")
    assert (as_root.exists("/src"), as_root.exists("/dst")) == (False, True)
