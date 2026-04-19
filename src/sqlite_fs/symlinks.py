from sqlite_fs.errors import NotFound


def insert(conn, inode, target):
    conn.execute(
        "INSERT INTO symlinks (inode, target) VALUES (?, ?)",
        (inode, target),
    )


def get(conn, inode):
    row = conn.execute(
        "SELECT target FROM symlinks WHERE inode = ?", (inode,),
    ).fetchone()
    if row is None:
        raise NotFound(f"inode {inode} has no symlinks row")
    return bytes(row[0])


def exists(conn, inode):
    row = conn.execute(
        "SELECT 1 FROM symlinks WHERE inode = ?", (inode,),
    ).fetchone()
    return row is not None
