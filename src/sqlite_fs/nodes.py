from dataclasses import dataclass

from sqlite_fs.errors import NotFound


@dataclass(frozen=True)
class NodeRow:
    inode: int
    kind: str
    mode: int
    uid: int
    gid: int
    size: int
    atime_ns: int
    mtime_ns: int
    ctime_ns: int
    nlink: int


def insert(conn, kind, mode, uid, gid, now_ns):
    """Insert a new node. Returns the new inode."""
    nlink = 2 if kind == "dir" else 1
    cur = conn.execute(
        """INSERT INTO nodes
           (kind, mode, uid, gid, size,
            atime_ns, mtime_ns, ctime_ns, nlink)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)""",
        (kind, mode, uid, gid, now_ns, now_ns, now_ns, nlink),
    )
    return cur.lastrowid


def get(conn, inode):
    row = conn.execute(
        """SELECT inode, kind, mode, uid, gid, size,
                  atime_ns, mtime_ns, ctime_ns, nlink
           FROM nodes WHERE inode = ?""",
        (inode,),
    ).fetchone()
    if row is None:
        raise NotFound(f"no node with inode {inode}")
    return NodeRow(*row)


def update_mode_uid_gid(conn, inode, *, mode=None, uid=None, gid=None, ctime_ns):
    fields = []
    values = []
    if mode is not None:
        fields.append("mode = ?"); values.append(mode)
    if uid is not None:
        fields.append("uid = ?"); values.append(uid)
    if gid is not None:
        fields.append("gid = ?"); values.append(gid)
    fields.append("ctime_ns = ?"); values.append(ctime_ns)
    values.append(inode)
    conn.execute(
        f"UPDATE nodes SET {', '.join(fields)} WHERE inode = ?",
        tuple(values),
    )


def update_times(conn, inode, *, atime_ns=None, mtime_ns=None, ctime_ns=None):
    fields = []
    values = []
    if atime_ns is not None:
        fields.append("atime_ns = ?"); values.append(atime_ns)
    if mtime_ns is not None:
        fields.append("mtime_ns = ?"); values.append(mtime_ns)
    if ctime_ns is not None:
        fields.append("ctime_ns = ?"); values.append(ctime_ns)
    if not fields:
        return
    values.append(inode)
    conn.execute(
        f"UPDATE nodes SET {', '.join(fields)} WHERE inode = ?",
        tuple(values),
    )


def update_size(conn, inode, size, mtime_ns, ctime_ns):
    conn.execute(
        "UPDATE nodes SET size = ?, mtime_ns = ?, ctime_ns = ? WHERE inode = ?",
        (size, mtime_ns, ctime_ns, inode),
    )


def change_nlink(conn, inode, delta, ctime_ns):
    conn.execute(
        "UPDATE nodes SET nlink = nlink + ?, ctime_ns = ? WHERE inode = ?",
        (delta, ctime_ns, inode),
    )
    (new_nlink,) = conn.execute(
        "SELECT nlink FROM nodes WHERE inode = ?", (inode,),
    ).fetchone()
    return new_nlink


def delete(conn, inode):
    conn.execute("DELETE FROM nodes WHERE inode = ?", (inode,))


def ancestry(conn, inode):
    """Return list of ancestor inodes walking up from inode. For the root,
    returns []. Uses entries.parent_of under the hood; only valid for dirs
    (which have exactly one parent entry)."""
    from sqlite_fs import entries
    result = []
    cur = inode
    while True:
        p = entries.parent_of(conn, cur)
        if p is None:
            break
        result.append(p)
        cur = p
    return result
