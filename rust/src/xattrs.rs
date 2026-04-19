use bitflags::bitflags;
use rusqlite::{Connection, OptionalExtension, params};

use crate::errors::{Error, Result};

pub const XATTR_NAME_MAX:  usize = 255;
pub const XATTR_VALUE_MAX: usize = 65536;

bitflags! {
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct XattrFlags: u32 {
        const CREATE  = 0x1;
        const REPLACE = 0x2;
    }
}

pub fn validate_name(name: &str, caller_uid: u32) -> Result<()> {
    if name.is_empty() {
        return Err(Error::InvalidXattr("xattr name must be non-empty".into()));
    }
    if name.contains('\0') {
        return Err(Error::InvalidXattr("xattr name contains NUL".into()));
    }
    if name.len() > XATTR_NAME_MAX {
        return Err(Error::InvalidXattr(format!(
            "xattr name exceeds {XATTR_NAME_MAX} bytes: {name:?}"
        )));
    }
    if !name.contains('.') {
        return Err(Error::InvalidXattr(format!(
            "xattr name must have a namespace: {name:?}"
        )));
    }
    if name.starts_with("trusted.") && caller_uid != 0 {
        return Err(Error::PermissionDenied(
            "setting trusted.* xattrs requires root".into(),
        ));
    }
    Ok(())
}

pub fn validate_value(value: &[u8]) -> Result<()> {
    if value.len() > XATTR_VALUE_MAX {
        return Err(Error::InvalidXattr(format!(
            "xattr value exceeds {XATTR_VALUE_MAX} bytes"
        )));
    }
    Ok(())
}

pub fn get(conn: &Connection, inode: u64, name: &str) -> Result<Vec<u8>> {
    let row: Option<Vec<u8>> = conn.query_row(
        "SELECT value FROM xattrs WHERE inode = ?1 AND name = ?2",
        params![inode as i64, name],
        |r| r.get(0),
    ).optional()?;
    row.ok_or_else(|| Error::NotFound(format!("xattr {name:?} not set on inode {inode}")))
}

pub fn set(
    conn: &Connection, inode: u64, name: &str, value: &[u8], flags: XattrFlags,
) -> Result<()> {
    let exists: bool = conn.query_row(
        "SELECT 1 FROM xattrs WHERE inode = ?1 AND name = ?2",
        params![inode as i64, name],
        |_| Ok(true),
    ).optional()?.unwrap_or(false);

    if flags.contains(XattrFlags::CREATE) && exists {
        return Err(Error::AlreadyExists(format!(
            "xattr {name:?} already exists on inode {inode}"
        )));
    }
    if flags.contains(XattrFlags::REPLACE) && !exists {
        return Err(Error::NotFound(format!(
            "xattr {name:?} not set on inode {inode}"
        )));
    }

    if exists {
        conn.execute(
            "UPDATE xattrs SET value = ?1 WHERE inode = ?2 AND name = ?3",
            params![value, inode as i64, name],
        )?;
    } else {
        conn.execute(
            "INSERT INTO xattrs (inode, name, value) VALUES (?1, ?2, ?3)",
            params![inode as i64, name, value],
        )?;
    }
    Ok(())
}

pub fn list_names(conn: &Connection, inode: u64) -> Result<Vec<String>> {
    let mut stmt = conn.prepare(
        "SELECT name FROM xattrs WHERE inode = ?1 ORDER BY name ASC",
    )?;
    let rows = stmt.query_map(params![inode as i64], |r| r.get::<_, String>(0))?;
    let mut out = Vec::new();
    for row in rows { out.push(row?); }
    Ok(out)
}

pub fn remove(conn: &Connection, inode: u64, name: &str) -> Result<()> {
    let changed = conn.execute(
        "DELETE FROM xattrs WHERE inode = ?1 AND name = ?2",
        params![inode as i64, name],
    )?;
    if changed == 0 {
        return Err(Error::NotFound(format!(
            "xattr {name:?} not set on inode {inode}"
        )));
    }
    Ok(())
}

pub fn has_any(conn: &Connection, inode: u64) -> Result<bool> {
    let row: Option<i64> = conn.query_row(
        "SELECT 1 FROM xattrs WHERE inode = ?1 LIMIT 1",
        params![inode as i64],
        |r| r.get(0),
    ).optional()?;
    Ok(row.is_some())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nodes;
    use crate::schema::{apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, SyncMode};
    use crate::types::NodeKind;

    fn fresh_file() -> (Connection, u64) {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        let ino = nodes::insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
        (conn, ino)
    }

    #[test]
    fn validate_name_rules() {
        assert!(validate_name("user.foo", 1000).is_ok());
        assert!(validate_name("", 1000).is_err());
        assert!(validate_name("noNamespace", 1000).is_err());
        assert!(validate_name("trusted.foo", 1000).is_err());
        assert!(validate_name("trusted.foo", 0).is_ok());
        assert!(validate_name(&format!("user.{}", "a".repeat(256)), 0).is_err());
    }

    #[test]
    fn set_get_remove_roundtrip() {
        let (conn, ino) = fresh_file();
        set(&conn, ino, "user.greeting", b"hello", XattrFlags::empty()).unwrap();
        assert_eq!(get(&conn, ino, "user.greeting").unwrap(), b"hello");
        assert_eq!(list_names(&conn, ino).unwrap(), vec!["user.greeting"]);
        remove(&conn, ino, "user.greeting").unwrap();
        assert!(matches!(get(&conn, ino, "user.greeting"), Err(Error::NotFound(_))));
    }

    #[test]
    fn create_flag_rejects_existing() {
        let (conn, ino) = fresh_file();
        set(&conn, ino, "user.k", b"v1", XattrFlags::empty()).unwrap();
        let err = set(&conn, ino, "user.k", b"v2", XattrFlags::CREATE).unwrap_err();
        assert!(matches!(err, Error::AlreadyExists(_)));
    }

    #[test]
    fn replace_flag_requires_existing() {
        let (conn, ino) = fresh_file();
        let err = set(&conn, ino, "user.k", b"v", XattrFlags::REPLACE).unwrap_err();
        assert!(matches!(err, Error::NotFound(_)));
    }

    #[test]
    fn validate_value_max() {
        assert!(validate_value(&vec![0u8; XATTR_VALUE_MAX]).is_ok());
        assert!(validate_value(&vec![0u8; XATTR_VALUE_MAX + 1]).is_err());
    }
}
