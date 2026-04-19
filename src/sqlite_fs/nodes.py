from dataclasses import dataclass
from typing import Optional

from sqlite_fs.errors import AlreadyExists, NotFound


@dataclass(frozen=True)
class NodeRow:
    inode: int
    parent: Optional[int]
    name: Optional[str]
    kind: str
    mode: int
    uid: int
    gid: int
    size: int
    atime_ns: int
    mtime_ns: int
    ctime_ns: int
    nlink: int


def insert(conn, parent, name, kind, mode, uid, gid, now_ns):
    nlink = 2 if kind == "dir" else 1
    cur = conn.execute(
        """INSERT INTO nodes
           (parent, name, kind, mode, uid, gid, size,
            atime_ns, mtime_ns, ctime_ns, nlink)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
        (parent, name, kind, mode, uid, gid, now_ns, now_ns, now_ns, nlink),
    )
    return cur.lastrowid


def get(conn, inode):
    row = conn.execute(
        """SELECT inode, parent, name, kind, mode, uid, gid, size,
                  atime_ns, mtime_ns, ctime_ns, nlink
           FROM nodes WHERE inode = ?""",
        (inode,),
    ).fetchone()
    if row is None:
        raise NotFound(f"no node with inode {inode}")
    return NodeRow(*row)


def get_child(conn, parent_inode, name):
    row = conn.execute(
        """SELECT inode, parent, name, kind, mode, uid, gid, size,
                  atime_ns, mtime_ns, ctime_ns, nlink
           FROM nodes WHERE parent = ? AND name = ?""",
        (parent_inode, name),
    ).fetchone()
    if row is None:
        raise NotFound(f"no child named {name!r} under inode {parent_inode}")
    return NodeRow(*row)


def list_children(conn, parent_inode):
    rows = conn.execute(
        """SELECT inode, parent, name, kind, mode, uid, gid, size,
                  atime_ns, mtime_ns, ctime_ns, nlink
           FROM nodes WHERE parent = ? ORDER BY name ASC""",
        (parent_inode,),
    ).fetchall()
    return [NodeRow(*r) for r in rows]


def count_children(conn, parent_inode, kind=None):
    if kind is None:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE parent = ?",
            (parent_inode,),
        ).fetchone()
    else:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE parent = ? AND kind = ?",
            (parent_inode, kind),
        ).fetchone()
    return n


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


def rename_entry(conn, inode, new_parent, new_name, ctime_ns):
    conn.execute(
        "UPDATE nodes SET parent = ?, name = ?, ctime_ns = ? WHERE inode = ?",
        (new_parent, new_name, ctime_ns, inode),
    )


def delete(conn, inode):
    conn.execute("DELETE FROM nodes WHERE inode = ?", (inode,))


def ancestry(conn, inode):
    result = []
    cur_inode = inode
    while True:
        row = conn.execute(
            "SELECT parent FROM nodes WHERE inode = ?", (cur_inode,),
        ).fetchone()
        if row is None or row[0] is None:
            break
        result.append(row[0])
        cur_inode = row[0]
    return result
