from sqlite_fs.types import FsckIssue, FsckReport


def run_fsck(conn):
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    integrity_result = "ok" if integ == "ok" else "corrupted"

    issues = []
    issues.extend(_check_orphan_blobs(conn))
    issues.extend(_check_orphan_xattrs(conn))
    issues.extend(_check_orphan_symlinks(conn))
    issues.extend(_check_dangling_parents(conn))
    issues.extend(_check_cycles(conn))
    issues.extend(_check_nlink(conn))

    return FsckReport(
        integrity_check_result=integrity_result,
        issues=issues,
    )


def _check_orphan_blobs(conn):
    rows = conn.execute("""
        SELECT DISTINCT b.inode FROM blobs b
        LEFT JOIN nodes n ON n.inode = b.inode
        WHERE n.inode IS NULL
    """).fetchall()
    return [
        FsckIssue(kind="orphan_blob", inode=r[0],
                  detail=f"blobs row for missing inode {r[0]}")
        for r in rows
    ]


def _check_orphan_xattrs(conn):
    rows = conn.execute("""
        SELECT DISTINCT x.inode FROM xattrs x
        LEFT JOIN nodes n ON n.inode = x.inode
        WHERE n.inode IS NULL
    """).fetchall()
    return [
        FsckIssue(kind="orphan_xattr", inode=r[0],
                  detail=f"xattrs row for missing inode {r[0]}")
        for r in rows
    ]


def _check_orphan_symlinks(conn):
    dangling = conn.execute("""
        SELECT DISTINCT s.inode FROM symlinks s
        LEFT JOIN nodes n ON n.inode = s.inode
        WHERE n.inode IS NULL
    """).fetchall()
    missing = conn.execute("""
        SELECT n.inode FROM nodes n
        LEFT JOIN symlinks s ON s.inode = n.inode
        WHERE n.kind = 'symlink' AND s.inode IS NULL
    """).fetchall()
    return (
        [FsckIssue(kind="orphan_symlink", inode=r[0],
                   detail=f"symlinks row for missing inode {r[0]}")
         for r in dangling]
        + [FsckIssue(kind="orphan_symlink", inode=r[0],
                     detail=f"nodes says symlink but no symlinks row for inode {r[0]}")
           for r in missing]
    )


def _check_dangling_parents(conn):
    # plan.v3: parents live in entries, not nodes.
    rows = conn.execute("""
        SELECT e.inode, e.parent FROM entries e
        LEFT JOIN nodes p ON p.inode = e.parent
        WHERE p.inode IS NULL
    """).fetchall()
    return [
        FsckIssue(kind="dangling_parent", inode=r[0],
                  detail=f"entry points at missing parent {r[1]}")
        for r in rows
    ]


def _check_cycles(conn):
    # plan.v3: walk via entries.parent.
    rows = conn.execute("""
        WITH RECURSIVE walk(inode, ancestor, depth) AS (
            SELECT inode, parent, 1 FROM entries
            UNION ALL
            SELECT w.inode, e.parent, w.depth + 1
            FROM walk w JOIN entries e ON e.inode = w.ancestor
            WHERE w.depth < 4096
        )
        SELECT DISTINCT inode FROM walk WHERE inode = ancestor
    """).fetchall()
    return [
        FsckIssue(kind="cycle", inode=r[0],
                  detail=f"inode {r[0]} is its own ancestor")
        for r in rows
    ]


def _check_nlink(conn):
    # plan.v3: a directory's nlink should equal 2 + count(child entries of kind='dir').
    rows = conn.execute("""
        SELECT n.inode, n.nlink,
               (SELECT COUNT(*) FROM entries e
                JOIN nodes c ON c.inode = e.inode
                WHERE e.parent = n.inode AND c.kind = 'dir') AS subdirs
        FROM nodes n
        WHERE n.kind = 'dir'
    """).fetchall()
    return [
        FsckIssue(kind="nlink_mismatch", inode=r[0],
                  detail=(f"dir {r[0]} has nlink={r[1]} but {r[2]} subdirs "
                          f"(expected {r[2] + 2})"))
        for r in rows
        if r[1] != r[2] + 2
    ]
