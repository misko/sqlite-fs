use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, params};

use crate::errors::{Error, Result};
use crate::fs::Filesystem;
use crate::schema::{
    apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, ROOT_INODE, SyncMode,
};

#[derive(Debug, Clone)]
pub struct MkfsOptions {
    pub chunk_size: i64,
    pub overwrite:  bool,
    pub owner_uid:  Option<u32>,
    pub owner_gid:  Option<u32>,
}

impl Default for MkfsOptions {
    fn default() -> Self {
        Self {
            chunk_size: DEFAULT_CHUNK_SIZE,
            overwrite:  false,
            owner_uid:  None,
            owner_gid:  None,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct OpenOptions {
    pub readonly:            bool,
    pub uid:                 Option<u32>,
    pub gid:                 Option<u32>,
    pub sync_mode:           SyncMode,
    pub checkpoint_interval: Option<Duration>,
}

pub fn mkfs(path: &Path, opts: MkfsOptions) -> Result<()> {
    if path.exists() {
        if !opts.overwrite {
            return Err(Error::AlreadyExists(
                format!("file exists: {}", path.display())
            ));
        }
        let _ = std::fs::remove_file(path);
        for suffix in ["-wal", "-shm"] {
            let base = path.file_name().map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default();
            let side = path.with_file_name(format!("{base}{suffix}"));
            let _ = std::fs::remove_file(side);
        }
    }

    let conn = Connection::open(path)?;
    apply_pragmas(&conn, SyncMode::Full)?;
    install_schema(&conn, opts.chunk_size)?;

    let owner_uid = opts.owner_uid.unwrap_or_else(|| unsafe { libc::geteuid() });
    let owner_gid = opts.owner_gid.unwrap_or_else(|| unsafe { libc::getegid() });

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64).unwrap_or(0);

    conn.execute(
        "INSERT INTO nodes (inode, kind, mode, uid, gid, size,
                            atime_ns, mtime_ns, ctime_ns, nlink)
         VALUES (?1, 'dir', ?2, ?3, ?4, 0, ?5, ?5, ?5, 2)",
        params![ROOT_INODE as i64, 0o755i64, owner_uid, owner_gid, now],
    )?;
    Ok(())
}

pub fn open_fs(path: &Path, opts: OpenOptions) -> Result<Filesystem> {
    let conn = if opts.readonly {
        Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )?
    } else {
        Connection::open(path)?
    };

    apply_pragmas_maybe_readonly(&conn, opts.sync_mode, opts.readonly)?;

    let uid = opts.uid.unwrap_or_else(|| unsafe { libc::geteuid() });
    let gid = opts.gid.unwrap_or_else(|| unsafe { libc::getegid() });

    let mut fs = Filesystem::new(conn, opts.readonly, uid, gid)?;
    if let Some(interval) = opts.checkpoint_interval {
        fs.start_checkpoint(path.to_path_buf(), interval);
    }
    Ok(fs)
}

fn apply_pragmas_maybe_readonly(
    conn: &Connection, sync_mode: SyncMode, readonly: bool,
) -> Result<()> {
    if !readonly {
        apply_pragmas(conn, sync_mode)?;
    } else {
        conn.execute_batch("PRAGMA foreign_keys = ON")?;
        conn.execute_batch("PRAGMA busy_timeout = 5000")?;
        conn.execute_batch("PRAGMA mmap_size = 0")?;
        conn.execute_batch("PRAGMA temp_store = MEMORY")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::NamedTempFile;

    #[test]
    fn mkfs_creates_usable_db() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        drop(tmp);
        mkfs(&path, MkfsOptions { overwrite: true, ..Default::default() }).unwrap();

        let fs = open_fs(&path, OpenOptions::default()).unwrap();
        let s = fs.stat("/").unwrap();
        assert_eq!(s.kind, crate::types::NodeKind::Dir);
        fs.close().unwrap();
    }

    #[test]
    fn mkfs_refuses_overwrite_by_default() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        drop(tmp);
        mkfs(&path, MkfsOptions { overwrite: true, ..Default::default() }).unwrap();
        let err = mkfs(&path, MkfsOptions::default()).unwrap_err();
        assert!(matches!(err, Error::AlreadyExists(_)));
    }

    #[test]
    fn mkdir_persists_across_reopen() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        drop(tmp);
        mkfs(&path, MkfsOptions { overwrite: true, ..Default::default() }).unwrap();

        {
            let mut fs = open_fs(&path, OpenOptions::default()).unwrap();
            fs.mkdir("/persisted", 0o755).unwrap();
            fs.close().unwrap();
        }
        {
            let fs = open_fs(&path, OpenOptions::default()).unwrap();
            let names: Vec<_> = fs.readdir("/").unwrap().into_iter().map(|e| e.name).collect();
            assert_eq!(names, vec!["persisted"]);
        }
    }
}
