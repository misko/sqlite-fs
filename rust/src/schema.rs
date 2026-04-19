use rusqlite::Connection;

use crate::errors::Result;

pub const SCHEMA_VERSION: i64 = 2;
pub const DEFAULT_CHUNK_SIZE: i64 = 65536;
pub const ROOT_INODE: u64 = 1;
pub const MAXSYMLINKS: u32 = 40;

pub const DDL: &str = r#"
CREATE TABLE schema_version (
    version INTEGER NOT NULL PRIMARY KEY,
    chunk_size INTEGER NOT NULL
);

CREATE TABLE nodes (
    inode INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'symlink')),
    mode INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    atime_ns INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    nlink INTEGER NOT NULL
);

CREATE TABLE entries (
    parent INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    name TEXT NOT NULL,
    inode INTEGER NOT NULL REFERENCES nodes(inode),
    PRIMARY KEY (parent, name)
);

CREATE INDEX entries_inode_idx ON entries (inode);

CREATE TABLE blobs (
    inode INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    chunk_id INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (inode, chunk_id)
);

CREATE TABLE xattrs (
    inode INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY (inode, name)
);

CREATE TABLE symlinks (
    inode INTEGER PRIMARY KEY REFERENCES nodes(inode) ON DELETE CASCADE,
    target BLOB NOT NULL
);
"#;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncMode { Full, Normal, Off }

impl SyncMode {
    pub fn pragma_value(&self) -> i32 {
        match self {
            SyncMode::Full => 2,
            SyncMode::Normal => 1,
            SyncMode::Off => 0,
        }
    }
}

impl Default for SyncMode {
    fn default() -> Self { SyncMode::Normal }
}

pub fn apply_pragmas(conn: &Connection, sync_mode: SyncMode) -> Result<()> {
    conn.execute_batch("PRAGMA journal_mode = WAL")?;
    conn.execute_batch(&format!(
        "PRAGMA synchronous = {}",
        sync_mode.pragma_value()
    ))?;
    conn.execute_batch("PRAGMA foreign_keys = ON")?;
    conn.execute_batch("PRAGMA busy_timeout = 5000")?;
    conn.execute_batch("PRAGMA mmap_size = 0")?;
    conn.execute_batch("PRAGMA temp_store = MEMORY")?;
    Ok(())
}

pub fn install_schema(conn: &Connection, chunk_size: i64) -> Result<()> {
    conn.execute_batch(DDL)?;
    conn.execute(
        "INSERT INTO schema_version (version, chunk_size) VALUES (?1, ?2)",
        rusqlite::params![SCHEMA_VERSION, chunk_size],
    )?;
    Ok(())
}

pub fn load_chunk_size(conn: &Connection) -> Result<i64> {
    let size: i64 = conn.query_row(
        "SELECT chunk_size FROM schema_version",
        [],
        |row| row.get(0),
    )?;
    Ok(size)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        conn
    }

    #[test]
    fn schema_installs_and_roundtrips_chunk_size() {
        let conn = fresh();
        assert_eq!(load_chunk_size(&conn).unwrap(), DEFAULT_CHUNK_SIZE);
    }

    #[test]
    fn default_sync_mode_is_normal() {
        assert_eq!(SyncMode::default(), SyncMode::Normal);
    }

    #[test]
    fn sync_mode_pragma_values() {
        assert_eq!(SyncMode::Full.pragma_value(), 2);
        assert_eq!(SyncMode::Normal.pragma_value(), 1);
        assert_eq!(SyncMode::Off.pragma_value(), 0);
    }

    #[test]
    fn integrity_check_ok_on_fresh_db() {
        let conn = fresh();
        let result: String = conn
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))
            .unwrap();
        assert_eq!(result, "ok");
    }
}
