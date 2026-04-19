use rusqlite::Connection;

use crate::errors::{Error, Result};
use crate::fdtable::{FdEntry, FdTable};
use crate::locks::LockManager;
use crate::paths::parse_path;
use crate::schema::{self, ROOT_INODE};
use crate::types::{Access, DirEntry, NodeKind, Stat};
use crate::{blobs, entries, nodes};

pub struct Filesystem {
    conn:        Connection,
    chunk_size:  u64,
    readonly:    bool,
    caller_uid:  u32,
    caller_gid:  u32,
    fdtable:     FdTable,
    locks:       LockManager,
}

fn now_ns() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64).unwrap_or(0)
}

impl Filesystem {
    pub fn new(conn: Connection, readonly: bool, uid: u32, gid: u32) -> Result<Self> {
        let chunk_size = schema::load_chunk_size(&conn)? as u64;
        Ok(Self {
            conn, chunk_size, readonly,
            caller_uid: uid, caller_gid: gid,
            fdtable: FdTable::new(),
            locks:   LockManager::new(),
        })
    }

    pub fn close(self) -> Result<()> {
        match self.conn.close() {
            Ok(())      => Ok(()),
            Err((_, e)) => Err(Error::Sqlite(e)),
        }
    }

    fn require_writable(&self) -> Result<()> {
        if self.readonly { return Err(Error::ReadOnlyFilesystem); }
        Ok(())
    }

    fn resolve_to_inode(&self, path: &str) -> Result<u64> {
        let components = parse_path(path)?;
        let mut cur = ROOT_INODE;
        for (i, name) in components.iter().enumerate() {
            let is_last = i + 1 == components.len();
            if !is_last {
                let row = nodes::get(&self.conn, cur)?;
                if row.kind != NodeKind::Dir {
                    return Err(Error::NotADirectory(format!(
                        "non-dir component in path {path:?}"
                    )));
                }
            }
            let entry = entries::get(&self.conn, cur, name)?;
            cur = entry.inode;
        }
        Ok(cur)
    }

    fn resolve_parent_and_name(&self, path: &str) -> Result<(u64, String)> {
        let mut components = parse_path(path)?;
        let name = components.pop().ok_or_else(|| Error::InvalidArgument(
            format!("cannot resolve parent of root or empty path: {path:?}")
        ))?;
        let parent = if components.is_empty() {
            ROOT_INODE
        } else {
            let parent_path = format!("/{}", components.join("/"));
            self.resolve_to_inode(&parent_path)?
        };
        Ok((parent, name))
    }

    pub fn stat(&self, path: &str) -> Result<Stat> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        Ok(Stat {
            kind:     row.kind,
            size:     row.size,
            mode:     row.mode,
            uid:      row.uid,
            gid:      row.gid,
            atime_ns: row.atime_ns,
            mtime_ns: row.mtime_ns,
            ctime_ns: row.ctime_ns,
            nlink:    row.nlink,
            inode:    row.inode,
        })
    }

    pub fn mkdir(&mut self, path: &str, mode: u32) -> Result<()> {
        self.require_writable()?;
        let (parent, name) = self.resolve_parent_and_name(path)?;
        let parent_row = nodes::get(&self.conn, parent)?;
        if parent_row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!(
                "parent of {path:?} is not a directory"
            )));
        }
        crate::perms::require_access(
            parent_row.mode, parent_row.uid, parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;

        let now = now_ns();
        let tx = self.conn.transaction()?;
        let new_ino = nodes::insert(
            &tx, NodeKind::Dir, mode, self.caller_uid, self.caller_gid, now,
        )?;
        entries::insert(&tx, parent, &name, new_ino)?;
        nodes::change_nlink(&tx, parent, 1, now)?;
        nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
        tx.commit()?;
        Ok(())
    }

    pub fn rmdir(&mut self, path: &str) -> Result<()> {
        self.require_writable()?;
        if path == "/" {
            return Err(Error::InvalidArgument("cannot rmdir root".into()));
        }
        let (parent, name) = self.resolve_parent_and_name(path)?;
        let entry = entries::get(&self.conn, parent, &name)?;
        let row = nodes::get(&self.conn, entry.inode)?;
        if row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!("{path:?} is not a directory")));
        }
        if entries::count(&self.conn, entry.inode, None)? > 0 {
            return Err(Error::DirectoryNotEmpty(format!("{path:?} is not empty")));
        }
        let parent_row = nodes::get(&self.conn, parent)?;
        crate::perms::require_access(
            parent_row.mode, parent_row.uid, parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;

        let now = now_ns();
        let tx = self.conn.transaction()?;
        entries::delete(&tx, parent, &name)?;
        nodes::change_nlink(&tx, parent, -1, now)?;
        nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
        nodes::delete(&tx, entry.inode)?;
        tx.commit()?;
        Ok(())
    }

    pub fn readdir(&self, path: &str) -> Result<Vec<DirEntry>> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        if row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!("{path:?} is not a directory")));
        }
        crate::perms::require_access(
            row.mode, row.uid, row.gid,
            self.caller_uid, self.caller_gid,
            Access::R,
        )?;
        let mut out = Vec::new();
        for e in entries::list(&self.conn, ino)? {
            let child = nodes::get(&self.conn, e.inode)?;
            out.push(DirEntry { name: e.name, kind: child.kind, inode: e.inode });
        }
        Ok(out)
    }

    pub fn create(&mut self, path: &str, flags: i32, mode: u32) -> Result<u64> {
        self.require_writable()?;
        let (parent, name) = self.resolve_parent_and_name(path)?;

        if let Ok(existing) = entries::get(&self.conn, parent, &name) {
            if flags & libc::O_EXCL != 0 {
                return Err(Error::AlreadyExists(format!("{path:?} already exists")));
            }
            let row = nodes::get(&self.conn, existing.inode)?;
            self.check_open_access(&row, flags)?;
            return Ok(self.fdtable.open(
                existing.inode, flags, self.caller_uid, self.caller_gid,
            ));
        }

        let parent_row = nodes::get(&self.conn, parent)?;
        if parent_row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!("parent of {path:?} is not a dir")));
        }
        crate::perms::require_access(
            parent_row.mode, parent_row.uid, parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;

        let now = now_ns();
        let new_ino = {
            let tx = self.conn.transaction()?;
            let new_ino = nodes::insert(
                &tx, NodeKind::File, mode & 0o7777,
                self.caller_uid, self.caller_gid, now,
            )?;
            entries::insert(&tx, parent, &name, new_ino)?;
            nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
            tx.commit()?;
            new_ino
        };

        Ok(self.fdtable.open(new_ino, flags, self.caller_uid, self.caller_gid))
    }

    fn check_open_access(&self, row: &crate::nodes::NodeRow, flags: i32) -> Result<()> {
        let mode = flags & libc::O_ACCMODE;
        let access = if mode == libc::O_RDONLY {
            Access::R
        } else if mode == libc::O_WRONLY {
            Access::W
        } else {
            Access::R | Access::W
        };
        crate::perms::require_access(
            row.mode, row.uid, row.gid,
            self.caller_uid, self.caller_gid,
            access,
        )
    }

    pub fn open(&mut self, path: &str, flags: i32) -> Result<u64> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        if row.kind == NodeKind::Dir {
            return Err(Error::IsADirectory(format!("{path:?} is a directory")));
        }
        self.check_open_access(&row, flags)?;
        if (flags & libc::O_TRUNC) != 0 {
            self.require_writable()?;
            let now = now_ns();
            let tx = self.conn.transaction()?;
            blobs::truncate_to(&tx, ino, 0, row.size, self.chunk_size)?;
            nodes::update_size(&tx, ino, 0, now, now)?;
            tx.commit()?;
        }
        Ok(self.fdtable.open(ino, flags, self.caller_uid, self.caller_gid))
    }

    pub fn write_fd(&mut self, fd: u64, offset: u64, data: &[u8]) -> Result<u64> {
        self.require_writable()?;
        let entry: FdEntry = self.fdtable.get(fd)?.clone();
        let mode = entry.flags & libc::O_ACCMODE;
        if mode != libc::O_WRONLY && mode != libc::O_RDWR {
            return Err(Error::PermissionDenied(format!(
                "fd {fd} not opened for write"
            )));
        }
        let row = nodes::get(&self.conn, entry.inode)?;
        let now = now_ns();

        let new_size = {
            let tx = self.conn.transaction()?;
            let new_size = blobs::write_range(
                &tx, entry.inode, data, offset, row.size, self.chunk_size,
            )?;
            nodes::update_size(&tx, entry.inode, new_size, now, now)?;
            tx.commit()?;
            new_size
        };
        Ok(new_size)
    }

    pub fn read_fd(&self, fd: u64, offset: u64, size: u64) -> Result<Vec<u8>> {
        let entry = self.fdtable.get(fd)?;
        let mode = entry.flags & libc::O_ACCMODE;
        if mode != libc::O_RDONLY && mode != libc::O_RDWR {
            return Err(Error::PermissionDenied(format!(
                "fd {fd} not opened for read"
            )));
        }
        let row = nodes::get(&self.conn, entry.inode)?;
        blobs::read_range(&self.conn, entry.inode, offset, size, row.size, self.chunk_size)
    }

    pub fn close_fd(&mut self, fd: u64) -> Result<()> {
        let inode = self.fdtable.close(fd)?;
        self.locks.on_fd_close(inode, fd as i64, 0);

        let row = nodes::get(&self.conn, inode)?;
        if row.nlink == 0 && self.fdtable.open_count(inode) == 0 {
            nodes::delete(&self.conn, inode)?;
        }
        Ok(())
    }

    pub fn unlink(&mut self, path: &str) -> Result<()> {
        self.require_writable()?;
        let (parent, name) = self.resolve_parent_and_name(path)?;
        let entry = entries::get(&self.conn, parent, &name)?;
        let row = nodes::get(&self.conn, entry.inode)?;
        if row.kind == NodeKind::Dir {
            return Err(Error::IsADirectory(format!("{path:?} is a directory")));
        }
        let parent_row = nodes::get(&self.conn, parent)?;
        crate::perms::require_access(
            parent_row.mode, parent_row.uid, parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;

        let now = now_ns();
        let new_nlink = {
            let tx = self.conn.transaction()?;
            entries::delete(&tx, parent, &name)?;
            let new_nlink = nodes::change_nlink(&tx, entry.inode, -1, now)?;
            nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
            tx.commit()?;
            new_nlink
        };
        if new_nlink == 0 && self.fdtable.open_count(entry.inode) == 0 {
            nodes::delete(&self.conn, entry.inode)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    use crate::schema::{apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, SyncMode};

    fn fresh_fs() -> Filesystem {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        let uid = unsafe { libc::geteuid() } as u32;
        let gid = unsafe { libc::getegid() } as u32;
        nodes::insert_with_inode(&conn, ROOT_INODE, NodeKind::Dir, 0o755, uid, gid, 0).unwrap();
        Filesystem::new(conn, false, uid, gid).unwrap()
    }

    #[test]
    fn stat_root_returns_dir() {
        let fs = fresh_fs();
        let s = fs.stat("/").unwrap();
        assert_eq!(s.kind, NodeKind::Dir);
        assert_eq!(s.inode, ROOT_INODE);
    }

    #[test]
    fn mkdir_creates_child() {
        let mut fs = fresh_fs();
        fs.mkdir("/foo", 0o755).unwrap();
        assert_eq!(fs.stat("/foo").unwrap().kind, NodeKind::Dir);
    }

    #[test]
    fn mkdir_parent_nlink_bumped() {
        let mut fs = fresh_fs();
        let before = fs.stat("/").unwrap().nlink;
        fs.mkdir("/sub", 0o755).unwrap();
        assert_eq!(fs.stat("/").unwrap().nlink, before + 1);
    }

    #[test]
    fn readdir_lists_children_sorted() {
        let mut fs = fresh_fs();
        fs.mkdir("/b", 0o755).unwrap();
        fs.mkdir("/a", 0o755).unwrap();
        fs.mkdir("/c", 0o755).unwrap();
        let names: Vec<_> = fs.readdir("/").unwrap().into_iter().map(|e| e.name).collect();
        assert_eq!(names, vec!["a", "b", "c"]);
    }

    #[test]
    fn create_write_read_roundtrip() {
        let mut fs = fresh_fs();
        let fd = fs.create("/hello.txt", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        let n = fs.write_fd(fd, 0, b"hello world").unwrap();
        assert_eq!(n, 11);
        let got = fs.read_fd(fd, 0, 5).unwrap();
        assert_eq!(got, b"hello");
        fs.close_fd(fd).unwrap();
    }

    #[test]
    fn write_extends_file_size_in_stat() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.write_fd(fd, 0, b"abc").unwrap();
        assert_eq!(fs.stat("/f").unwrap().size, 3);
    }

    #[test]
    fn create_excl_rejects_existing() {
        let mut fs = fresh_fs();
        fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        let err = fs.create(
            "/f", libc::O_CREAT | libc::O_EXCL | libc::O_RDWR, 0o644,
        ).unwrap_err();
        assert!(matches!(err, Error::AlreadyExists(_)));
    }

    #[test]
    fn unlink_removes_file() {
        let mut fs = fresh_fs();
        let fd = fs.create("/gone", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        fs.unlink("/gone").unwrap();
        assert!(matches!(fs.stat("/gone"), Err(Error::NotFound(_))));
    }

    #[test]
    fn unlink_while_open_defers_gc() {
        let mut fs = fresh_fs();
        let fd = fs.create("/x", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.write_fd(fd, 0, b"data").unwrap();
        fs.unlink("/x").unwrap();
        let got = fs.read_fd(fd, 0, 4).unwrap();
        assert_eq!(got, b"data");
        fs.close_fd(fd).unwrap();
    }

    #[test]
    fn rmdir_requires_empty() {
        let mut fs = fresh_fs();
        fs.mkdir("/d", 0o755).unwrap();
        let fd = fs.create("/d/inside", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        let err = fs.rmdir("/d").unwrap_err();
        assert!(matches!(err, Error::DirectoryNotEmpty(_)));
        fs.unlink("/d/inside").unwrap();
        fs.rmdir("/d").unwrap();
    }

    #[test]
    fn readonly_rejects_mutation() {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        let uid = unsafe { libc::geteuid() } as u32;
        let gid = unsafe { libc::getegid() } as u32;
        nodes::insert_with_inode(&conn, ROOT_INODE, NodeKind::Dir, 0o755, uid, gid, 0).unwrap();
        let mut fs = Filesystem::new(conn, true, uid, gid).unwrap();
        let err = fs.mkdir("/nope", 0o755).unwrap_err();
        assert!(matches!(err, Error::ReadOnlyFilesystem));
    }
}
