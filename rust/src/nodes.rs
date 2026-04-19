use rusqlite::{Connection, OptionalExtension, params};

use crate::errors::{Error, Result};
use crate::types::NodeKind;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeRow {
    pub inode:    u64,
    pub kind:     NodeKind,
    pub mode:     u32,
    pub uid:      u32,
    pub gid:      u32,
    pub size:     u64,
    pub atime_ns: i64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub nlink:    u32,
}

fn kind_from_str(s: &str) -> NodeKind {
    NodeKind::from_str(s).expect("invalid kind in DB")
}

pub fn insert(
    conn: &Connection, kind: NodeKind, mode: u32, uid: u32, gid: u32, now_ns: i64,
) -> Result<u64> {
    let nlink: u32 = if kind == NodeKind::Dir { 2 } else { 1 };
    conn.execute(
        "INSERT INTO nodes (kind, mode, uid, gid, size,
                            atime_ns, mtime_ns, ctime_ns, nlink)
         VALUES (?1, ?2, ?3, ?4, 0, ?5, ?5, ?5, ?6)",
        params![kind.as_str(), mode, uid, gid, now_ns, nlink],
    )?;
    Ok(conn.last_insert_rowid() as u64)
}

pub fn insert_with_inode(
    conn: &Connection, inode: u64, kind: NodeKind, mode: u32,
    uid: u32, gid: u32, now_ns: i64,
) -> Result<()> {
    let nlink: u32 = if kind == NodeKind::Dir { 2 } else { 1 };
    conn.execute(
        "INSERT INTO nodes (inode, kind, mode, uid, gid, size,
                            atime_ns, mtime_ns, ctime_ns, nlink)
         VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6, ?6, ?6, ?7)",
        params![inode as i64, kind.as_str(), mode, uid, gid, now_ns, nlink],
    )?;
    Ok(())
}

pub fn get(conn: &Connection, inode: u64) -> Result<NodeRow> {
    let row = conn.query_row(
        "SELECT inode, kind, mode, uid, gid, size,
                atime_ns, mtime_ns, ctime_ns, nlink
         FROM nodes WHERE inode = ?1",
        params![inode as i64],
        |r| Ok(NodeRow {
            inode:    r.get::<_, i64>(0)? as u64,
            kind:     kind_from_str(&r.get::<_, String>(1)?),
            mode:     r.get::<_, i64>(2)? as u32,
            uid:      r.get::<_, i64>(3)? as u32,
            gid:      r.get::<_, i64>(4)? as u32,
            size:     r.get::<_, i64>(5)? as u64,
            atime_ns: r.get(6)?,
            mtime_ns: r.get(7)?,
            ctime_ns: r.get(8)?,
            nlink:    r.get::<_, i64>(9)? as u32,
        }),
    ).optional()?;
    row.ok_or_else(|| Error::NotFound(format!("no node with inode {inode}")))
}

pub fn update_mode_uid_gid(
    conn: &Connection, inode: u64,
    mode: Option<u32>, uid: Option<u32>, gid: Option<u32>, ctime_ns: i64,
) -> Result<()> {
    let mut clauses: Vec<&str> = Vec::new();
    let mut vals: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(m) = mode { clauses.push("mode = ?"); vals.push((m as i64).into()); }
    if let Some(u) = uid  { clauses.push("uid = ?");  vals.push((u as i64).into()); }
    if let Some(g) = gid  { clauses.push("gid = ?");  vals.push((g as i64).into()); }
    clauses.push("ctime_ns = ?");
    vals.push(ctime_ns.into());
    vals.push((inode as i64).into());
    let sql = format!("UPDATE nodes SET {} WHERE inode = ?", clauses.join(", "));
    conn.execute(&sql, rusqlite::params_from_iter(vals))?;
    Ok(())
}

pub fn update_times(
    conn: &Connection, inode: u64,
    atime_ns: Option<i64>, mtime_ns: Option<i64>, ctime_ns: Option<i64>,
) -> Result<()> {
    let mut clauses: Vec<&str> = Vec::new();
    let mut vals: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(a) = atime_ns { clauses.push("atime_ns = ?"); vals.push(a.into()); }
    if let Some(m) = mtime_ns { clauses.push("mtime_ns = ?"); vals.push(m.into()); }
    if let Some(c) = ctime_ns { clauses.push("ctime_ns = ?"); vals.push(c.into()); }
    if clauses.is_empty() { return Ok(()); }
    vals.push((inode as i64).into());
    let sql = format!("UPDATE nodes SET {} WHERE inode = ?", clauses.join(", "));
    conn.execute(&sql, rusqlite::params_from_iter(vals))?;
    Ok(())
}

pub fn update_size(
    conn: &Connection, inode: u64, size: u64, mtime_ns: i64, ctime_ns: i64,
) -> Result<()> {
    conn.execute(
        "UPDATE nodes SET size = ?1, mtime_ns = ?2, ctime_ns = ?3 WHERE inode = ?4",
        params![size as i64, mtime_ns, ctime_ns, inode as i64],
    )?;
    Ok(())
}

pub fn change_nlink(conn: &Connection, inode: u64, delta: i32, ctime_ns: i64) -> Result<u32> {
    conn.execute(
        "UPDATE nodes SET nlink = nlink + ?1, ctime_ns = ?2 WHERE inode = ?3",
        params![delta, ctime_ns, inode as i64],
    )?;
    let new_nlink: i64 = conn.query_row(
        "SELECT nlink FROM nodes WHERE inode = ?1",
        params![inode as i64],
        |r| r.get(0),
    )?;
    Ok(new_nlink as u32)
}

pub fn delete(conn: &Connection, inode: u64) -> Result<()> {
    conn.execute("DELETE FROM nodes WHERE inode = ?1", params![inode as i64])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, SyncMode};

    fn fresh() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        conn
    }

    #[test]
    fn insert_and_get_file_roundtrips() {
        let conn = fresh();
        let inode = insert(&conn, NodeKind::File, 0o644, 1000, 1000, 42).unwrap();
        let row = get(&conn, inode).unwrap();
        assert_eq!(row.kind, NodeKind::File);
        assert_eq!(row.mode, 0o644);
        assert_eq!(row.nlink, 1);
        assert_eq!(row.atime_ns, 42);
        assert_eq!(row.mtime_ns, 42);
        assert_eq!(row.ctime_ns, 42);
        assert_eq!(row.size, 0);
    }

    #[test]
    fn dir_starts_with_nlink_two() {
        let conn = fresh();
        let inode = insert(&conn, NodeKind::Dir, 0o755, 0, 0, 1).unwrap();
        assert_eq!(get(&conn, inode).unwrap().nlink, 2);
    }

    #[test]
    fn get_missing_returns_not_found() {
        let conn = fresh();
        assert!(matches!(get(&conn, 999), Err(Error::NotFound(_))));
    }

    #[test]
    fn change_nlink_round_trip() {
        let conn = fresh();
        let inode = insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
        assert_eq!(change_nlink(&conn, inode, 1, 2).unwrap(), 2);
        assert_eq!(change_nlink(&conn, inode, -1, 3).unwrap(), 1);
    }

    #[test]
    fn update_mode_preserves_other_fields() {
        let conn = fresh();
        let inode = insert(&conn, NodeKind::File, 0o644, 1000, 1000, 1).unwrap();
        update_mode_uid_gid(&conn, inode, Some(0o600), None, None, 5).unwrap();
        let row = get(&conn, inode).unwrap();
        assert_eq!(row.mode, 0o600);
        assert_eq!(row.uid, 1000);
        assert_eq!(row.ctime_ns, 5);
    }
}
