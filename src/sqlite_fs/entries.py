from dataclasses import dataclass

from sqlite_fs.errors import NotFound


@dataclass(frozen=True)
class EntryRow:
    parent: int
    name: str
    inode: int


def insert(conn, parent, name, inode):
    conn.execute(
        "INSERT INTO entries (parent, name, inode) VALUES (?, ?, ?)",
        (parent, name, inode),
    )


def get(conn, parent, name):
    row = conn.execute(
        "SELECT parent, name, inode FROM entries WHERE parent = ? AND name = ?",
        (parent, name),
    ).fetchone()
    if row is None:
        raise NotFound(f"no entry {name!r} under inode {parent}")
    return EntryRow(*row)


def delete(conn, parent, name):
    conn.execute(
        "DELETE FROM entries WHERE parent = ? AND name = ?",
        (parent, name),
    )


def list_(conn, parent):
    rows = conn.execute(
        "SELECT parent, name, inode FROM entries WHERE parent = ? ORDER BY name ASC",
        (parent,),
    ).fetchall()
    return [EntryRow(*r) for r in rows]


def count(conn, parent, kind=None):
    if kind is None:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE parent = ?",
            (parent,),
        ).fetchone()
    else:
        (n,) = conn.execute(
            """SELECT COUNT(*) FROM entries e
               JOIN nodes n ON n.inode = e.inode
               WHERE e.parent = ? AND n.kind = ?""",
            (parent, kind),
        ).fetchone()
    return n


def rename(conn, old_parent, old_name, new_parent, new_name):
    conn.execute(
        """UPDATE entries SET parent = ?, name = ?
           WHERE parent = ? AND name = ?""",
        (new_parent, new_name, old_parent, old_name),
    )


def parent_of(conn, inode):
    row = conn.execute(
        "SELECT parent FROM entries WHERE inode = ? LIMIT 1", (inode,),
    ).fetchone()
    return None if row is None else row[0]
