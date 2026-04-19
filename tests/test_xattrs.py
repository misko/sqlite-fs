import os
import pytest

from sqlite_fs.errors import (
    AlreadyExists,
    InvalidXattr,
    NotFound,
    PermissionDenied,
)


XATTR_CREATE = os.XATTR_CREATE
XATTR_REPLACE = os.XATTR_REPLACE


@pytest.fixture
def fs_with_file(as_root):
    fd = as_root.create("/f")
    as_root.close_fd(fd)
    yield as_root


def test_set_and_get_roundtrip(fs_with_file):
    fs_with_file.setxattr("/f", "user.tag", b"project-alpha")
    assert fs_with_file.getxattr("/f", "user.tag") == b"project-alpha"


def test_getxattr_missing_raises(fs_with_file):
    with pytest.raises(NotFound):
        fs_with_file.getxattr("/f", "user.missing")


def test_listxattr_empty_for_new_file(fs_with_file):
    assert fs_with_file.listxattr("/f") == []


def test_listxattr_returns_all_names(fs_with_file):
    fs_with_file.setxattr("/f", "user.a", b"1")
    fs_with_file.setxattr("/f", "user.b", b"2")
    fs_with_file.setxattr("/f", "user.c", b"3")
    assert sorted(fs_with_file.listxattr("/f")) == ["user.a", "user.b", "user.c"]


def test_setxattr_create_flag_on_existing_raises(fs_with_file):
    fs_with_file.setxattr("/f", "user.x", b"first")
    with pytest.raises(AlreadyExists):
        fs_with_file.setxattr("/f", "user.x", b"second", flags=XATTR_CREATE)
    assert fs_with_file.getxattr("/f", "user.x") == b"first"


def test_setxattr_replace_flag_on_missing_raises(fs_with_file):
    with pytest.raises(NotFound):
        fs_with_file.setxattr("/f", "user.missing", b"v", flags=XATTR_REPLACE)


def test_setxattr_default_flag_is_upsert(fs_with_file):
    fs_with_file.setxattr("/f", "user.x", b"one")
    fs_with_file.setxattr("/f", "user.x", b"two")
    assert fs_with_file.getxattr("/f", "user.x") == b"two"


def test_removexattr_missing_raises(fs_with_file):
    with pytest.raises(NotFound):
        fs_with_file.removexattr("/f", "user.missing")


def test_removexattr_actually_removes(fs_with_file):
    fs_with_file.setxattr("/f", "user.x", b"v")
    fs_with_file.removexattr("/f", "user.x")
    assert "user.x" not in fs_with_file.listxattr("/f")
    with pytest.raises(NotFound):
        fs_with_file.getxattr("/f", "user.x")


def test_xattrs_isolated_per_inode(as_root):
    fd_a = as_root.create("/a"); as_root.close_fd(fd_a)
    fd_b = as_root.create("/b"); as_root.close_fd(fd_b)

    as_root.setxattr("/a", "user.tag", b"aaa")
    assert as_root.listxattr("/b") == []
    with pytest.raises(NotFound):
        as_root.getxattr("/b", "user.tag")


def test_value_too_large_raises(fs_with_file):
    ok = b"a" * 65536
    fs_with_file.setxattr("/f", "user.big", ok)
    assert fs_with_file.getxattr("/f", "user.big") == ok

    too_big = b"a" * 65537
    with pytest.raises(InvalidXattr):
        fs_with_file.setxattr("/f", "user.bigger", too_big)


def test_name_too_long_raises(fs_with_file):
    ok = "user." + ("a" * 250)
    fs_with_file.setxattr("/f", ok, b"v")

    too_long = "user." + ("a" * 251)
    with pytest.raises(InvalidXattr):
        fs_with_file.setxattr("/f", too_long, b"v")


def test_namespace_trusted_requires_root(fresh_fs):
    with fresh_fs.as_user(0, 0):
        fd = fresh_fs.create("/f", mode=0o666)
        fresh_fs.close_fd(fd)

    with fresh_fs.as_user(1000, 1000):
        with pytest.raises(PermissionDenied):
            fresh_fs.setxattr("/f", "trusted.something", b"v")

    with fresh_fs.as_user(0, 0):
        fresh_fs.setxattr("/f", "trusted.something", b"v")
        assert fresh_fs.getxattr("/f", "trusted.something") == b"v"


def test_namespace_user_unrestricted(fresh_fs):
    with fresh_fs.as_user(0, 0):
        fd = fresh_fs.create("/f", mode=0o666)
        fresh_fs.close_fd(fd)

    with fresh_fs.as_user(1000, 1000):
        fresh_fs.setxattr("/f", "user.tag", b"v")
        assert fresh_fs.getxattr("/f", "user.tag") == b"v"


def test_xattrs_on_directory(as_root):
    as_root.mkdir("/d")
    as_root.setxattr("/d", "user.tag", b"directory-tag")
    assert as_root.getxattr("/d", "user.tag") == b"directory-tag"


def test_xattrs_survive_reopen(tmp_db):
    from sqlite_fs import mkfs, open_fs

    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fd = fs.create("/f"); fs.close_fd(fd)
        fs.setxattr("/f", "user.k", b"persisted")
    fs.close()

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        assert fs.getxattr("/f", "user.k") == b"persisted"
    fs.close()
