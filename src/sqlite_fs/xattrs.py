import os

from sqlite_fs.errors import (
    AlreadyExists,
    InvalidXattr,
    NotFound,
    PermissionDenied,
)


XATTR_NAME_MAX = 255
XATTR_VALUE_MAX = 65536


def validate_name(name, caller_uid):
    if not name:
        raise InvalidXattr("xattr name must be non-empty")
    if "\x00" in name:
        raise InvalidXattr("xattr name contains NUL")
    if len(name.encode("utf-8")) > XATTR_NAME_MAX:
        raise InvalidXattr(
            f"xattr name exceeds {XATTR_NAME_MAX} bytes: {name!r}"
        )
    if "." not in name:
        raise InvalidXattr(f"xattr name must have a namespace: {name!r}")
    if name.startswith("trusted.") and caller_uid != 0:
        raise PermissionDenied(
            "setting trusted.* xattrs requires root"
        )


def validate_value(value):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise InvalidXattr(
            f"xattr value must be bytes, got {type(value).__name__}"
        )
    if len(value) > XATTR_VALUE_MAX:
        raise InvalidXattr(
            f"xattr value exceeds {XATTR_VALUE_MAX} bytes"
        )


def get(conn, inode, name):
    row = conn.execute(
        "SELECT value FROM xattrs WHERE inode = ? AND name = ?",
        (inode, name),
    ).fetchone()
    if row is None:
        raise NotFound(f"xattr {name!r} not set on inode {inode}")
    return bytes(row[0])


def set(conn, inode, name, value, flags):
    exists = conn.execute(
        "SELECT 1 FROM xattrs WHERE inode = ? AND name = ?",
        (inode, name),
    ).fetchone() is not None

    if flags & os.XATTR_CREATE and exists:
        raise AlreadyExists(f"xattr {name!r} already exists on inode {inode}")
    if flags & os.XATTR_REPLACE and not exists:
        raise NotFound(f"xattr {name!r} not set on inode {inode}")

    if exists:
        conn.execute(
            "UPDATE xattrs SET value = ? WHERE inode = ? AND name = ?",
            (value, inode, name),
        )
    else:
        conn.execute(
            "INSERT INTO xattrs (inode, name, value) VALUES (?, ?, ?)",
            (inode, name, value),
        )


def list_names(conn, inode):
    rows = conn.execute(
        "SELECT name FROM xattrs WHERE inode = ? ORDER BY name ASC",
        (inode,),
    ).fetchall()
    return [r[0] for r in rows]


def remove(conn, inode, name):
    cur = conn.execute(
        "DELETE FROM xattrs WHERE inode = ? AND name = ?",
        (inode, name),
    )
    if cur.rowcount == 0:
        raise NotFound(f"xattr {name!r} not set on inode {inode}")


def has_any(conn, inode):
    row = conn.execute(
        "SELECT 1 FROM xattrs WHERE inode = ? LIMIT 1", (inode,),
    ).fetchone()
    return row is not None
