import os
import sqlite3
import time

from sqlite_fs.errors import AlreadyExists
from sqlite_fs.schema import (
    DEFAULT_CHUNK_SIZE,
    ROOT_INODE,
    apply_pragmas,
    install_schema,
)


def mkfs(path, *, chunk_size=DEFAULT_CHUNK_SIZE, overwrite=False,
         owner_uid=None, owner_gid=None):
    """Create a new sqlite-fs filesystem.

    plan.v3 finding: the root directory is owned by the calling euid/egid
    by default, not uid=0. Otherwise a non-root user who runs mkfs cannot
    later mkdir/create at root without root-bypass. Explicit
    `owner_uid=0` still available for a 'system' filesystem.
    """
    if owner_uid is None:
        owner_uid = os.geteuid()
    if owner_gid is None:
        owner_gid = os.getegid()

    if os.path.exists(path):
        if not overwrite:
            raise AlreadyExists(f"file exists: {path}")
        os.unlink(path)
        for suffix in ("-wal", "-shm"):
            side = path + suffix
            if os.path.exists(side):
                os.unlink(side)

    conn = sqlite3.connect(path)
    try:
        apply_pragmas(conn)
        install_schema(conn, chunk_size)
        now = time.time_ns()
        # plan.v3: nodes has no parent/name columns; root has no entry.
        conn.execute(
            """INSERT INTO nodes (inode, kind, mode, uid, gid, size,
                                  atime_ns, mtime_ns, ctime_ns, nlink)
               VALUES (?, 'dir', ?, ?, ?, 0, ?, ?, ?, 2)""",
            (ROOT_INODE, 0o755, owner_uid, owner_gid, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def open_fs(path, *, readonly=False, uid=None, gid=None, sync_mode="full"):
    """Open an existing sqlite-fs filesystem.

    `sync_mode`:
      - 'full'   (default) — fsync per commit; idea.md durability contract.
      - 'normal' — WAL-safe but last transaction may be lost on power loss.
      - 'off'    — DANGEROUS; only for scratch / unit tests.
    """
    from sqlite_fs.fs import Filesystem

    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    uid = os.geteuid() if uid is None else uid
    gid = os.getegid() if gid is None else gid
    return Filesystem(conn, readonly=readonly, uid=uid, gid=gid,
                      sync_mode=sync_mode)
