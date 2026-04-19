import pytest

from sqlite_fs.perms import check_access, require_access
from sqlite_fs.types import Access
from sqlite_fs.errors import PermissionDenied


def test_owner_read_on_readable_file():
    assert check_access(
        node_mode=0o600, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.R,
    ) is True


def test_owner_write_on_readonly_file():
    assert check_access(
        node_mode=0o400, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.W,
    ) is False


def test_group_member_read():
    assert check_access(
        node_mode=0o040, node_uid=1000, node_gid=2000,
        caller_uid=9999, caller_gid=2000,
        access=Access.R,
    ) is True


def test_group_not_owner_uses_group_bits_not_owner():
    # Owner can do anything (0o700); group can do nothing (0o000).
    # Caller is in the group but is not the owner.
    assert check_access(
        node_mode=0o700, node_uid=1000, node_gid=2000,
        caller_uid=9999, caller_gid=2000,
        access=Access.R,
    ) is False


def test_other_read():
    assert check_access(
        node_mode=0o004, node_uid=1000, node_gid=1000,
        caller_uid=9999, caller_gid=9999,
        access=Access.R,
    ) is True


def test_other_denied_when_only_group_allows():
    assert check_access(
        node_mode=0o040, node_uid=1000, node_gid=2000,
        caller_uid=9999, caller_gid=9999,
        access=Access.R,
    ) is False


def test_root_bypasses_read_and_write():
    assert check_access(
        node_mode=0o000, node_uid=1000, node_gid=1000,
        caller_uid=0, caller_gid=0,
        access=Access.R | Access.W,
    ) is True


def test_root_execute_requires_at_least_one_x_bit():
    # No execute bit at all — root denied.
    assert check_access(
        node_mode=0o666, node_uid=1000, node_gid=1000,
        caller_uid=0, caller_gid=0,
        access=Access.X,
    ) is False

    # Any execute bit set — root allowed.
    assert check_access(
        node_mode=0o001, node_uid=1000, node_gid=1000,
        caller_uid=0, caller_gid=0,
        access=Access.X,
    ) is True


def test_combined_access_requires_every_bit():
    # 0o400: r--. Asking R|W fails because W missing.
    assert check_access(
        node_mode=0o400, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.R | Access.W,
    ) is False

    # 0o600: rw-. Asking R|W succeeds.
    assert check_access(
        node_mode=0o600, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.R | Access.W,
    ) is True


def test_execute_on_directory_means_traversal():
    # Directory with 0o100 (only owner-execute): owner may traverse.
    assert check_access(
        node_mode=0o100, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.X,
    ) is True

    # Same directory, read requested: denied.
    assert check_access(
        node_mode=0o100, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.R,
    ) is False


def test_check_access_returns_bool_not_raises():
    result = check_access(
        node_mode=0o000, node_uid=1000, node_gid=1000,
        caller_uid=9999, caller_gid=9999,
        access=Access.R,
    )
    assert result is False


def test_require_access_raises_permission_denied():
    # Allowed — no exception.
    require_access(
        node_mode=0o600, node_uid=1000, node_gid=1000,
        caller_uid=1000, caller_gid=1000,
        access=Access.R,
    )

    with pytest.raises(PermissionDenied):
        require_access(
            node_mode=0o000, node_uid=1000, node_gid=1000,
            caller_uid=9999, caller_gid=9999,
            access=Access.R,
        )
