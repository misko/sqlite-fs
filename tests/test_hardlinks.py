import pytest

from sqlite_fs.errors import (
    AlreadyExists,
    NotFound,
    PermissionDenied,
)


def test_link_creates_hard_link(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"content", offset=0)
    as_root.close_fd(fd)

    as_root.link("/a", "/b")

    assert as_root.stat("/a").inode == as_root.stat("/b").inode
    assert as_root.stat("/a").nlink == 2
    assert as_root.stat("/b").nlink == 2

    fd = as_root.open("/b", flags=0)
    assert as_root.read(fd, size=7, offset=0) == b"content"
    as_root.close_fd(fd)


def test_link_cross_directory(as_root):
    as_root.mkdir("/dir1")
    as_root.mkdir("/dir2")
    fd = as_root.create("/dir1/f"); as_root.close_fd(fd)

    as_root.link("/dir1/f", "/dir2/f")
    assert as_root.stat("/dir1/f").inode == as_root.stat("/dir2/f").inode


def test_link_to_existing_dest_raises(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    fd = as_root.create("/b"); as_root.close_fd(fd)
    with pytest.raises(AlreadyExists):
        as_root.link("/a", "/b")


def test_link_to_nonexistent_src_raises(as_root):
    with pytest.raises(NotFound):
        as_root.link("/no-such", "/b")


def test_link_to_directory_forbidden(as_root):
    as_root.mkdir("/d")
    with pytest.raises(PermissionDenied):
        as_root.link("/d", "/d-link")


def test_unlink_decrements_nlink(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    as_root.link("/a", "/b")
    as_root.link("/a", "/c")
    assert as_root.stat("/a").nlink == 3

    as_root.unlink("/b")
    assert as_root.stat("/a").nlink == 2
    assert as_root.exists("/b") is False
    assert as_root.exists("/c")


def test_unlink_last_link_gcs_inode(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"payload", offset=0)
    as_root.close_fd(fd)
    as_root.setxattr("/a", "user.tag", b"v")

    inode = as_root.stat("/a").inode

    assert as_root._row_exists("nodes", inode) is True
    assert as_root._count_chunks(inode) == 1
    assert as_root._row_exists("xattrs", inode) is True

    as_root.unlink("/a")

    assert as_root._row_exists("nodes", inode) is False
    assert as_root._count_chunks(inode) == 0
    assert as_root._row_exists("xattrs", inode) is False


def test_open_fd_delays_gc_past_unlink(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"still here", offset=0)
    inode = as_root.stat("/a").inode

    as_root.unlink("/a")
    assert as_root.exists("/a") is False
    assert as_root._row_exists("nodes", inode) is True

    assert as_root.read(fd, size=10, offset=0) == b"still here"

    as_root.close_fd(fd)
    assert as_root._row_exists("nodes", inode) is False


def test_hard_link_xattrs_shared(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    as_root.link("/a", "/b")

    as_root.setxattr("/a", "user.tag", b"value-via-a")
    assert as_root.getxattr("/b", "user.tag") == b"value-via-a"

    as_root.setxattr("/b", "user.other", b"value-via-b")
    assert as_root.getxattr("/a", "user.other") == b"value-via-b"


def test_hard_link_content_shared(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    as_root.link("/a", "/b")

    import os
    fd_a = as_root.open("/a", flags=os.O_WRONLY)
    as_root.write(fd_a, b"shared", offset=0)
    as_root.close_fd(fd_a)

    fd_b = as_root.open("/b", flags=0)
    assert as_root.read(fd_b, size=6, offset=0) == b"shared"
    as_root.close_fd(fd_b)


def test_hard_link_independent_metadata_paths(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    as_root.link("/a", "/b")

    as_root.rename("/a", "/a2")
    assert as_root.exists("/a") is False
    assert as_root.exists("/a2")
    assert as_root.exists("/b")
    assert as_root.stat("/a2").inode == as_root.stat("/b").inode


def test_nlink_file_starts_at_one(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    assert as_root.stat("/a").nlink == 1


def test_gc_does_not_run_while_fd_held_through_close(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"contents", offset=0)
    inode = as_root.stat("/a").inode

    as_root.unlink("/a")
    assert as_root._row_exists("nodes", inode) is True

    as_root.write(fd, b"more", offset=8)
    assert as_root.read(fd, size=12, offset=0) == b"contentsmore"

    as_root.close_fd(fd)
    assert as_root._row_exists("nodes", inode) is False
