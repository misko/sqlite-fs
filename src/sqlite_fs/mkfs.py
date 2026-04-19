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


def mkfs(path, *, chunk_size=DEFAULT_CHUNK_SIZE, overwrite=False):
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
               VALUES (?, 'dir', ?, 0, 0, 0, ?, ?, ?, 2)""",
            (ROOT_INODE, 0o755, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def open_fs(path, *, readonly=False, uid=None, gid=None):
    # Imported here to avoid circular import at module load.
    from sqlite_fs.fs import Filesystem

    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    uid = os.geteuid() if uid is None else uid
    gid = os.getegid() if gid is None else gid
    return Filesystem(conn, readonly=readonly, uid=uid, gid=gid)
