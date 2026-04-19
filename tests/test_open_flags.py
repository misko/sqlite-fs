import os
import pytest

from sqlite_fs.errors import (
    AlreadyExists,
    NotFound,
    PermissionDenied,
    SymlinkLoop,
    BadFileDescriptor,
)


def test_open_rdonly_on_existing(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"x", offset=0)
    as_root.close_fd(fd)

    fd = as_root.open("/a", flags=os.O_RDONLY)
    assert as_root.read(fd, size=1, offset=0) == b"x"
    with pytest.raises(PermissionDenied):
        as_root.write(fd, b"y", offset=0)
    as_root.close_fd(fd)


def test_open_wronly_allows_write_denies_read(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)

    fd = as_root.open("/a", flags=os.O_WRONLY)
    as_root.write(fd, b"wronly", offset=0)
    with pytest.raises(PermissionDenied):
        as_root.read(fd, size=1, offset=0)
    as_root.close_fd(fd)


def test_open_rdwr_allows_both(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)

    fd = as_root.open("/a", flags=os.O_RDWR)
    as_root.write(fd, b"rw", offset=0)
    assert as_root.read(fd, size=2, offset=0) == b"rw"
    as_root.close_fd(fd)


def test_open_nonexistent_without_creat_raises(as_root):
    with pytest.raises(NotFound):
        as_root.open("/no-such", flags=os.O_RDONLY)


def test_open_creat_creates_if_missing(as_root):
    fd = as_root.open("/new", flags=os.O_CREAT | os.O_WRONLY, mode=0o644)
    as_root.write(fd, b"created", offset=0)
    as_root.close_fd(fd)

    assert as_root.exists("/new")
    assert as_root.stat("/new").mode & 0o777 == 0o644


def test_open_creat_on_existing_is_fine(as_root):
    fd = as_root.create("/a", mode=0o644)
    as_root.write(fd, b"original", offset=0)
    as_root.close_fd(fd)

    fd = as_root.open("/a", flags=os.O_CREAT | os.O_RDONLY, mode=0o777)
    assert as_root.read(fd, size=8, offset=0) == b"original"
    as_root.close_fd(fd)
    assert as_root.stat("/a").mode & 0o777 == 0o644


def test_open_creat_excl_on_existing_raises(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    with pytest.raises(AlreadyExists):
        as_root.open("/a", flags=os.O_CREAT | os.O_EXCL, mode=0o644)


def test_open_trunc_on_existing(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"old content here", offset=0)
    as_root.close_fd(fd)
    assert as_root.stat("/a").size == 16

    fd = as_root.open("/a", flags=os.O_WRONLY | os.O_TRUNC)
    assert as_root.stat("/a").size == 0
    as_root.close_fd(fd)


def test_open_trunc_requires_write(as_root):
    fd = as_root.create("/a", mode=0o644)
    as_root.write(fd, b"x" * 100, offset=0)
    as_root.close_fd(fd)

    fd = as_root.open("/a", flags=os.O_RDONLY | os.O_TRUNC)
    assert as_root.stat("/a").size == 0
    as_root.close_fd(fd)


def test_open_nofollow_on_symlink_raises(as_root):
    fd = as_root.create("/target"); as_root.close_fd(fd)
    as_root.symlink(b"/target", "/link")

    with pytest.raises(SymlinkLoop):
        as_root.open("/link", flags=os.O_RDONLY | os.O_NOFOLLOW)

    fd = as_root.open("/link", flags=os.O_RDONLY)
    as_root.close_fd(fd)


def test_open_direct_sync_nonblock_accepted_silently(as_root):
    fd = as_root.create("/a")
    as_root.write(fd, b"x", offset=0)
    as_root.close_fd(fd)

    flags = os.O_RDONLY | os.O_DIRECT | os.O_SYNC | os.O_NONBLOCK
    fd = as_root.open("/a", flags=flags)
    assert as_root.read(fd, size=1, offset=0) == b"x"
    as_root.close_fd(fd)


def test_close_fd_twice_raises(as_root):
    fd = as_root.create("/a")
    as_root.close_fd(fd)
    with pytest.raises(BadFileDescriptor):
        as_root.close_fd(fd)


def test_read_bad_fd_raises(as_root):
    with pytest.raises(BadFileDescriptor):
        as_root.read(99999, size=10, offset=0)
