use rusqlite::{Connection, OptionalExtension, params};

use crate::errors::{Error, Result};
use crate::types::NodeKind;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntryRow {
    pub parent: u64,
    pub name:   String,
    pub inode:  u64,
}

pub fn insert(conn: &Connection, parent: u64, name: &str, inode: u64) -> Result<()> {
    let res = conn.execute(
        "INSERT INTO entries (parent, name, inode) VALUES (?1, ?2, ?3)",
        params![parent as i64, name, inode as i64],
    );
    match res {
        Ok(_) => Ok(()),
        Err(rusqlite::Error::SqliteFailure(e, _))
            if e.code == rusqlite::ErrorCode::ConstraintViolation =>
        {
            Err(Error::AlreadyExists(format!(
                "entry {name:?} already exists under inode {parent}"
            )))
        }
        Err(e) => Err(Error::Sqlite(e)),
    }
}

pub fn get(conn: &Connection, parent: u64, name: &str) -> Result<EntryRow> {
    let row = conn.query_row(
        "SELECT parent, name, inode FROM entries WHERE parent = ?1 AND name = ?2",
        params![parent as i64, name],
        |r| Ok(EntryRow {
            parent: r.get::<_, i64>(0)? as u64,
            name:   r.get(1)?,
            inode:  r.get::<_, i64>(2)? as u64,
        }),
    ).optional()?;
    row.ok_or_else(|| Error::NotFound(format!("no entry {name:?} under inode {parent}")))
}

pub fn delete(conn: &Connection, parent: u64, name: &str) -> Result<()> {
    conn.execute(
        "DELETE FROM entries WHERE parent = ?1 AND name = ?2",
        params![parent as i64, name],
    )?;
    Ok(())
}

pub fn list(conn: &Connection, parent: u64) -> Result<Vec<EntryRow>> {
    let mut stmt = conn.prepare(
        "SELECT parent, name, inode FROM entries WHERE parent = ?1 ORDER BY name ASC",
    )?;
    let rows = stmt.query_map(params![parent as i64], |r| Ok(EntryRow {
        parent: r.get::<_, i64>(0)? as u64,
        name:   r.get(1)?,
        inode:  r.get::<_, i64>(2)? as u64,
    }))?;
    let mut out = Vec::new();
    for row in rows { out.push(row?); }
    Ok(out)
}

pub fn count(conn: &Connection, parent: u64, kind: Option<NodeKind>) -> Result<u64> {
    let n: i64 = match kind {
        None => conn.query_row(
            "SELECT COUNT(*) FROM entries WHERE parent = ?1",
            params![parent as i64],
            |r| r.get(0),
        )?,
        Some(k) => conn.query_row(
            "SELECT COUNT(*) FROM entries e
             JOIN nodes n ON n.inode = e.inode
             WHERE e.parent = ?1 AND n.kind = ?2",
            params![parent as i64, k.as_str()],
            |r| r.get(0),
        )?,
    };
    Ok(n as u64)
}

pub fn rename(
    conn: &Connection,
    old_parent: u64, old_name: &str,
    new_parent: u64, new_name: &str,
) -> Result<()> {
    let res = conn.execute(
        "UPDATE entries SET parent = ?1, name = ?2 WHERE parent = ?3 AND name = ?4",
        params![new_parent as i64, new_name, old_parent as i64, old_name],
    );
    match res {
        Ok(_) => Ok(()),
        Err(rusqlite::Error::SqliteFailure(e, _))
            if e.code == rusqlite::ErrorCode::ConstraintViolation =>
        {
            Err(Error::AlreadyExists(format!(
                "entry {new_name:?} already exists under inode {new_parent}"
            )))
        }
        Err(e) => Err(Error::Sqlite(e)),
    }
}

pub fn parent_of(conn: &Connection, inode: u64) -> Result<Option<u64>> {
    let row: Option<i64> = conn.query_row(
        "SELECT parent FROM entries WHERE inode = ?1 LIMIT 1",
        params![inode as i64],
        |r| r.get(0),
    ).optional()?;
    Ok(row.map(|v| v as u64))
}

pub fn ancestry(conn: &Connection, inode: u64) -> Result<Vec<u64>> {
    let mut out = Vec::new();
    let mut cur = inode;
    loop {
        match parent_of(conn, cur)? {
            Some(p) => { out.push(p); cur = p; }
            None    => break,
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nodes;
    use crate::schema::{apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, SyncMode, ROOT_INODE};

    fn fresh_with_root() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        nodes::insert_with_inode(&conn, ROOT_INODE, NodeKind::Dir, 0o755, 0, 0, 1).unwrap();
        conn
    }

    #[test]
    fn insert_and_get_roundtrips() {
        let conn = fresh_with_root();
        let ino = nodes::insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
        insert(&conn, ROOT_INODE, "foo", ino).unwrap();
        let row = get(&conn, ROOT_INODE, "foo").unwrap();
        assert_eq!(row.inode, ino);
        assert_eq!(row.name, "foo");
    }

    #[test]
    fn duplicate_entry_rejected() {
        let conn = fresh_with_root();
        let ino = nodes::insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
        insert(&conn, ROOT_INODE, "foo", ino).unwrap();
        let err = insert(&conn, ROOT_INODE, "foo", ino).unwrap_err();
        assert!(matches!(err, Error::AlreadyExists(_)));
    }

    #[test]
    fn list_returns_entries_sorted() {
        let conn = fresh_with_root();
        for name in ["b", "a", "c"] {
            let ino = nodes::insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
            insert(&conn, ROOT_INODE, name, ino).unwrap();
        }
        let names: Vec<_> = list(&conn, ROOT_INODE).unwrap()
            .into_iter().map(|e| e.name).collect();
        assert_eq!(names, vec!["a", "b", "c"]);
    }

    #[test]
    fn count_by_kind() {
        let conn = fresh_with_root();
        let f = nodes::insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
        let d = nodes::insert(&conn, NodeKind::Dir,  0o755, 0, 0, 1).unwrap();
        insert(&conn, ROOT_INODE, "f", f).unwrap();
        insert(&conn, ROOT_INODE, "d", d).unwrap();
        assert_eq!(count(&conn, ROOT_INODE, None).unwrap(), 2);
        assert_eq!(count(&conn, ROOT_INODE, Some(NodeKind::File)).unwrap(), 1);
        assert_eq!(count(&conn, ROOT_INODE, Some(NodeKind::Dir)).unwrap(), 1);
    }

    #[test]
    fn parent_of_walks_ancestry() {
        let conn = fresh_with_root();
        let d1 = nodes::insert(&conn, NodeKind::Dir, 0o755, 0, 0, 1).unwrap();
        let d2 = nodes::insert(&conn, NodeKind::Dir, 0o755, 0, 0, 1).unwrap();
        insert(&conn, ROOT_INODE, "d1", d1).unwrap();
        insert(&conn, d1, "d2", d2).unwrap();
        assert_eq!(ancestry(&conn, d2).unwrap(), vec![d1, ROOT_INODE]);
    }
}
