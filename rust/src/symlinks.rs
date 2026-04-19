use rusqlite::{Connection, OptionalExtension, params};

use crate::errors::{Error, Result};

pub fn insert(conn: &Connection, inode: u64, target: &[u8]) -> Result<()> {
    conn.execute(
        "INSERT INTO symlinks (inode, target) VALUES (?1, ?2)",
        params![inode as i64, target],
    )?;
    Ok(())
}

pub fn get(conn: &Connection, inode: u64) -> Result<Vec<u8>> {
    let row: Option<Vec<u8>> = conn.query_row(
        "SELECT target FROM symlinks WHERE inode = ?1",
        params![inode as i64],
        |r| r.get(0),
    ).optional()?;
    row.ok_or_else(|| Error::NotFound(format!("inode {inode} has no symlinks row")))
}

pub fn exists(conn: &Connection, inode: u64) -> Result<bool> {
    let found: Option<i64> = conn.query_row(
        "SELECT 1 FROM symlinks WHERE inode = ?1",
        params![inode as i64],
        |r| r.get(0),
    ).optional()?;
    Ok(found.is_some())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nodes;
    use crate::schema::{apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, SyncMode};
    use crate::types::NodeKind;

    fn fresh_symlink(target: &[u8]) -> (Connection, u64) {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        let ino = nodes::insert(&conn, NodeKind::Symlink, 0o777, 0, 0, 1).unwrap();
        insert(&conn, ino, target).unwrap();
        (conn, ino)
    }

    #[test]
    fn roundtrip_utf8() {
        let (conn, ino) = fresh_symlink(b"/tmp/foo");
        assert_eq!(get(&conn, ino).unwrap(), b"/tmp/foo");
        assert!(exists(&conn, ino).unwrap());
    }

    #[test]
    fn roundtrip_non_utf8() {
        let (conn, ino) = fresh_symlink(&[0xff, 0xfe, 0x00, 0x7f]);
        assert_eq!(get(&conn, ino).unwrap(), &[0xff, 0xfe, 0x00, 0x7f]);
    }

    #[test]
    fn get_missing_not_found() {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        assert!(matches!(get(&conn, 999), Err(Error::NotFound(_))));
        assert!(!exists(&conn, 999).unwrap());
    }
}
