from sqlite_fs.errors import PermissionDenied
from sqlite_fs.types import Access


def check_access(node_mode, node_uid, node_gid, caller_uid, caller_gid, access):
    # Root bypass — conditional on execute.
    if caller_uid == 0:
        if Access.X in access:
            any_x = (node_mode & 0o111) != 0
            if not any_x:
                return False
        return True

    # Non-root: pick the triple (owner / group / other).
    if caller_uid == node_uid:
        bits = (node_mode >> 6) & 0o7
    elif caller_gid == node_gid:
        bits = (node_mode >> 3) & 0o7
    else:
        bits = node_mode & 0o7

    required = access.value
    return (bits & required) == required


def require_access(node_mode, node_uid, node_gid, caller_uid, caller_gid, access):
    if not check_access(node_mode, node_uid, node_gid,
                        caller_uid, caller_gid, access):
        raise PermissionDenied(
            f"access {access!r} denied: "
            f"caller=({caller_uid},{caller_gid}), "
            f"node=({node_uid},{node_gid},0o{node_mode:o})"
        )
