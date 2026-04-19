use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use rusqlite::Connection;

use crate::errors::{Error, Result};
use crate::fdtable::{FdEntry, FdTable};
use crate::locks::LockManager;
use crate::paths::parse_path;
use crate::schema::{self, ROOT_INODE};
use crate::types::{Access, DirEntry, Event, EventKind, FlockOp, LockOp, LockQuery, NodeKind, Stat};
use crate::watch::{self, WatchRegistry, Watcher};
use crate::{blobs, entries, nodes};

pub struct Filesystem {
    conn:        Connection,
    chunk_size:  u64,
    readonly:    bool,
    caller_uid:  u32,
    caller_gid:  u32,
    fdtable:     FdTable,
    locks:       LockManager,
    watchers:    Arc<Mutex<WatchRegistry>>,
    checkpoint:  Option<CheckpointHandle>,
}

pub(crate) struct CheckpointHandle {
    stop:   mpsc::Sender<()>,
    handle: Option<thread::JoinHandle<()>>,
}

impl CheckpointHandle {
    pub fn stop(mut self) {
        let _ = self.stop.send(());
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

fn start_checkpoint_thread(db_path: PathBuf, interval: Duration) -> CheckpointHandle {
    let (tx, rx) = mpsc::channel::<()>();
    let handle = thread::spawn(move || {
        let conn = match Connection::open(&db_path) {
            Ok(c) => c,
            Err(_) => return,
        };
        loop {
            match rx.recv_timeout(interval) {
                Ok(_) => break,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    let _: rusqlite::Result<()> =
                        conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE)");
                }
            }
        }
    });
    CheckpointHandle { stop: tx, handle: Some(handle) }
}

pub struct AsUserGuard<'a> {
    fs:       &'a mut Filesystem,
    prev_uid: u32,
    prev_gid: u32,
}

impl std::ops::Deref for AsUserGuard<'_> {
    type Target = Filesystem;
    fn deref(&self) -> &Filesystem { self.fs }
}

impl std::ops::DerefMut for AsUserGuard<'_> {
    fn deref_mut(&mut self) -> &mut Filesystem { self.fs }
}

impl Drop for AsUserGuard<'_> {
    fn drop(&mut self) {
        self.fs.caller_uid = self.prev_uid;
        self.fs.caller_gid = self.prev_gid;
    }
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
            watchers: Arc::new(Mutex::new(WatchRegistry::new())),
            checkpoint: None,
        })
    }

    pub fn start_checkpoint(&mut self, db_path: PathBuf, interval: Duration) {
        if self.readonly || self.checkpoint.is_some() { return; }
        self.checkpoint = Some(start_checkpoint_thread(db_path, interval));
    }

    pub fn as_user(&mut self, uid: u32, gid: u32) -> AsUserGuard<'_> {
        let prev_uid = self.caller_uid;
        let prev_gid = self.caller_gid;
        self.caller_uid = uid;
        self.caller_gid = gid;
        AsUserGuard { fs: self, prev_uid, prev_gid }
    }

    pub fn path_of_inode(&self, inode: u64) -> Option<String> {
        if inode == ROOT_INODE { return Some("/".into()); }
        self.path_for_inode(inode)
    }

    fn emit(
        &self, kind: EventKind, path: &str, node_kind: NodeKind, inode: u64,
        src_path: Option<String>, dst_path: Option<String>,
    ) {
        let ev = Event {
            kind, path: path.to_string(), src_path, dst_path,
            node_kind, inode, timestamp_ns: watch::now_ns(),
        };
        if let Ok(reg) = self.watchers.lock() {
            reg.emit(ev);
        }
    }

    pub fn watch(&self, path: &str, recursive: bool) -> Result<Watcher> {
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
        Ok(WatchRegistry::subscribe(&self.watchers, path.to_string(), recursive))
    }

    fn path_for_inode(&self, inode: u64) -> Option<String> {
        let (mut parent, name): (u64, String) = self.conn.query_row(
            "SELECT parent, name FROM entries WHERE inode = ?1 LIMIT 1",
            rusqlite::params![inode as i64],
            |r| Ok((r.get::<_, i64>(0)? as u64, r.get::<_, String>(1)?)),
        ).ok()?;
        let mut parts = vec![name];
        while parent != ROOT_INODE {
            let (gp, gn): (u64, String) = self.conn.query_row(
                "SELECT parent, name FROM entries WHERE inode = ?1 LIMIT 1",
                rusqlite::params![parent as i64],
                |r| Ok((r.get::<_, i64>(0)? as u64, r.get::<_, String>(1)?)),
            ).ok()?;
            parts.push(gn);
            parent = gp;
        }
        parts.reverse();
        Some(format!("/{}", parts.join("/")))
    }

    pub fn close(mut self) -> Result<()> {
        if let Some(ckpt) = self.checkpoint.take() {
            ckpt.stop();
        }
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
        let new_ino = {
            let tx = self.conn.transaction()?;
            let new_ino = nodes::insert(
                &tx, NodeKind::Dir, mode, self.caller_uid, self.caller_gid, now,
            )?;
            entries::insert(&tx, parent, &name, new_ino)?;
            nodes::change_nlink(&tx, parent, 1, now)?;
            nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
            tx.commit()?;
            new_ino
        };
        self.emit(EventKind::Create, path, NodeKind::Dir, new_ino, None, None);
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
        {
            let tx = self.conn.transaction()?;
            entries::delete(&tx, parent, &name)?;
            nodes::change_nlink(&tx, parent, -1, now)?;
            nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
            nodes::delete(&tx, entry.inode)?;
            tx.commit()?;
        }
        self.emit(EventKind::Remove, path, NodeKind::Dir, entry.inode, None, None);
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
        self.emit(EventKind::Create, path, NodeKind::File, new_ino, None, None);
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
        if let Some(path) = self.path_for_inode(entry.inode) {
            self.emit(EventKind::Modify, &path, NodeKind::File, entry.inode, None, None);
        }
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
        self.emit(EventKind::Remove, path, row.kind, entry.inode, None, None);
        Ok(())
    }

    pub fn symlink(&mut self, target: &[u8], path: &str) -> Result<()> {
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
        let new_ino = {
            let tx = self.conn.transaction()?;
            let new_ino = nodes::insert(
                &tx, NodeKind::Symlink, 0o777, self.caller_uid, self.caller_gid, now,
            )?;
            entries::insert(&tx, parent, &name, new_ino)?;
            crate::symlinks::insert(&tx, new_ino, target)?;
            nodes::update_times(&tx, parent, None, Some(now), Some(now))?;
            tx.commit()?;
            new_ino
        };
        self.emit(EventKind::Create, path, NodeKind::Symlink, new_ino, None, None);
        Ok(())
    }

    pub fn readlink(&self, path: &str) -> Result<Vec<u8>> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        if row.kind != NodeKind::Symlink {
            return Err(Error::InvalidArgument(format!(
                "{path:?} is not a symlink"
            )));
        }
        crate::symlinks::get(&self.conn, ino)
    }

    pub fn link(&mut self, target: &str, link_path: &str) -> Result<()> {
        self.require_writable()?;
        let target_ino = self.resolve_to_inode(target)?;
        let target_row = nodes::get(&self.conn, target_ino)?;
        if target_row.kind == NodeKind::Dir {
            return Err(Error::IsADirectory(format!(
                "cannot hard-link directory {target:?}"
            )));
        }

        let (link_parent, link_name) = self.resolve_parent_and_name(link_path)?;
        let link_parent_row = nodes::get(&self.conn, link_parent)?;
        if link_parent_row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!(
                "parent of {link_path:?} is not a directory"
            )));
        }
        crate::perms::require_access(
            link_parent_row.mode, link_parent_row.uid, link_parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;

        let now = now_ns();
        {
            let tx = self.conn.transaction()?;
            entries::insert(&tx, link_parent, &link_name, target_ino)?;
            nodes::change_nlink(&tx, target_ino, 1, now)?;
            nodes::update_times(&tx, link_parent, None, Some(now), Some(now))?;
            tx.commit()?;
        }
        self.emit(EventKind::Create, link_path, target_row.kind, target_ino, None, None);
        Ok(())
    }

    pub fn rename(&mut self, src: &str, dst: &str) -> Result<()> {
        self.require_writable()?;
        if src == dst { return Ok(()); }

        let (src_parent, src_name) = self.resolve_parent_and_name(src)?;
        let src_entry = entries::get(&self.conn, src_parent, &src_name)?;
        let src_row   = nodes::get(&self.conn, src_entry.inode)?;

        let (dst_parent, dst_name) = self.resolve_parent_and_name(dst)?;
        let dst_parent_row = nodes::get(&self.conn, dst_parent)?;
        if dst_parent_row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!(
                "parent of {dst:?} is not a directory"
            )));
        }
        let src_parent_row = nodes::get(&self.conn, src_parent)?;
        crate::perms::require_access(
            src_parent_row.mode, src_parent_row.uid, src_parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;
        crate::perms::require_access(
            dst_parent_row.mode, dst_parent_row.uid, dst_parent_row.gid,
            self.caller_uid, self.caller_gid,
            Access::W | Access::X,
        )?;

        if src_row.kind == NodeKind::Dir {
            if src_entry.inode == dst_parent {
                return Err(Error::InvalidArgument(
                    "cannot rename directory into itself".into()
                ));
            }
            let ancestry = entries::ancestry(&self.conn, dst_parent)?;
            if ancestry.contains(&src_entry.inode) {
                return Err(Error::InvalidArgument(
                    "cannot rename directory into its own subtree".into()
                ));
            }
        }

        let existing_dst = entries::get(&self.conn, dst_parent, &dst_name).ok();

        let now = now_ns();
        let tx = self.conn.transaction()?;

        if let Some(dst_entry) = existing_dst {
            if dst_entry.inode == src_entry.inode {
                // Renaming onto itself via a hard link — POSIX no-op.
                tx.commit()?;
                return Ok(());
            }
            let dst_row = nodes::get(&tx, dst_entry.inode)?;
            match (src_row.kind, dst_row.kind) {
                (NodeKind::Dir, NodeKind::Dir) => {
                    if entries::count(&tx, dst_entry.inode, None)? > 0 {
                        return Err(Error::DirectoryNotEmpty(format!(
                            "rename target {dst:?} is a non-empty directory"
                        )));
                    }
                }
                (NodeKind::Dir, _) => {
                    return Err(Error::NotADirectory(format!(
                        "rename target {dst:?} is not a directory"
                    )));
                }
                (_, NodeKind::Dir) => {
                    return Err(Error::IsADirectory(format!(
                        "rename target {dst:?} is a directory"
                    )));
                }
                _ => {}
            }
            entries::delete(&tx, dst_parent, &dst_name)?;
            let new_nlink = nodes::change_nlink(&tx, dst_entry.inode, -1, now)?;
            if dst_row.kind == NodeKind::Dir && src_parent != dst_parent {
                nodes::change_nlink(&tx, dst_parent, -1, now)?;
            }
            if new_nlink == 0 {
                nodes::delete(&tx, dst_entry.inode)?;
            }
        }

        entries::rename(&tx, src_parent, &src_name, dst_parent, &dst_name)?;

        if src_row.kind == NodeKind::Dir && src_parent != dst_parent {
            nodes::change_nlink(&tx, src_parent, -1, now)?;
            nodes::change_nlink(&tx, dst_parent, 1, now)?;
        }
        nodes::update_times(&tx, src_parent, None, Some(now), Some(now))?;
        if src_parent != dst_parent {
            nodes::update_times(&tx, dst_parent, None, Some(now), Some(now))?;
        }
        nodes::update_times(&tx, src_entry.inode, None, None, Some(now))?;

        tx.commit()?;
        self.emit(
            EventKind::Move, dst, src_row.kind, src_entry.inode,
            Some(src.to_string()), Some(dst.to_string()),
        );
        Ok(())
    }

    pub fn chmod(&mut self, path: &str, mode: u32) -> Result<()> {
        self.require_writable()?;
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        if self.caller_uid != 0 && self.caller_uid != row.uid {
            return Err(Error::PermissionDenied(format!(
                "chmod on {path:?}: not owner"
            )));
        }
        let now = now_ns();
        nodes::update_mode_uid_gid(&self.conn, ino, Some(mode & 0o7777), None, None, now)?;
        self.emit(EventKind::Metadata, path, row.kind, ino, None, None);
        Ok(())
    }

    pub fn chown(&mut self, path: &str, uid: Option<u32>, gid: Option<u32>) -> Result<()> {
        self.require_writable()?;
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        let changing_uid = matches!(uid, Some(u) if u != row.uid);
        if changing_uid && self.caller_uid != 0 {
            return Err(Error::PermissionDenied(
                "changing uid requires root".into()
            ));
        }
        if gid.is_some() && self.caller_uid != 0 && self.caller_uid != row.uid {
            return Err(Error::PermissionDenied(
                "changing gid requires owner or root".into()
            ));
        }
        let now = now_ns();
        nodes::update_mode_uid_gid(&self.conn, ino, None, uid, gid, now)?;
        self.emit(EventKind::Metadata, path, row.kind, ino, None, None);
        Ok(())
    }

    pub fn utimes(
        &mut self, path: &str, atime_ns: Option<i64>, mtime_ns: Option<i64>,
    ) -> Result<()> {
        self.require_writable()?;
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        if self.caller_uid != 0 && self.caller_uid != row.uid {
            return Err(Error::PermissionDenied(format!(
                "utimes on {path:?}: not owner"
            )));
        }
        let now = now_ns();
        nodes::update_times(&self.conn, ino, atime_ns, mtime_ns, Some(now))?;
        self.emit(EventKind::Metadata, path, row.kind, ino, None, None);
        Ok(())
    }

    pub fn setxattr(
        &mut self, path: &str, name: &str, value: &[u8],
        flags: crate::xattrs::XattrFlags,
    ) -> Result<()> {
        self.require_writable()?;
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        crate::perms::require_access(
            row.mode, row.uid, row.gid,
            self.caller_uid, self.caller_gid, Access::W,
        )?;
        crate::xattrs::validate_name(name, self.caller_uid)?;
        crate::xattrs::validate_value(value)?;

        crate::xattrs::set(&self.conn, ino, name, value, flags)?;
        let now = watch::now_ns();
        nodes::update_times(&self.conn, ino, None, None, Some(now))?;
        self.emit(EventKind::Metadata, path, row.kind, ino, None, None);
        Ok(())
    }

    pub fn getxattr(&self, path: &str, name: &str) -> Result<Vec<u8>> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        crate::perms::require_access(
            row.mode, row.uid, row.gid,
            self.caller_uid, self.caller_gid, Access::R,
        )?;
        crate::xattrs::get(&self.conn, ino, name)
    }

    pub fn listxattr(&self, path: &str) -> Result<Vec<String>> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        crate::perms::require_access(
            row.mode, row.uid, row.gid,
            self.caller_uid, self.caller_gid, Access::R,
        )?;
        crate::xattrs::list_names(&self.conn, ino)
    }

    pub fn removexattr(&mut self, path: &str, name: &str) -> Result<()> {
        self.require_writable()?;
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        crate::perms::require_access(
            row.mode, row.uid, row.gid,
            self.caller_uid, self.caller_gid, Access::W,
        )?;
        crate::xattrs::remove(&self.conn, ino, name)?;
        let now = watch::now_ns();
        nodes::update_times(&self.conn, ino, None, None, Some(now))?;
        self.emit(EventKind::Metadata, path, row.kind, ino, None, None);
        Ok(())
    }

    pub fn posix_lock(
        &mut self, fd: u64, pid: i64, op: LockOp,
        start: u64, length: u64, wait: bool,
    ) -> Result<()> {
        let entry = self.fdtable.get(fd)?;
        let inode = entry.inode;
        self.locks.posix_lock(inode, fd as i64, pid, op, start, length, wait)
    }

    pub fn ofd_lock(
        &mut self, fd: u64, op: LockOp, start: u64, length: u64, wait: bool,
    ) -> Result<()> {
        let entry = self.fdtable.get(fd)?;
        let inode = entry.inode;
        self.locks.ofd_lock(inode, fd as i64, op, start, length, wait)
    }

    pub fn flock(&mut self, fd: u64, op: FlockOp, wait: bool) -> Result<()> {
        let entry = self.fdtable.get(fd)?;
        let inode = entry.inode;
        self.locks.flock(inode, fd as i64, op, wait)
    }

    pub fn posix_getlk(
        &self, fd: u64, pid: i64, start: u64, length: u64,
    ) -> Result<Option<LockQuery>> {
        let entry = self.fdtable.get(fd)?;
        Ok(self.locks.posix_getlk(entry.inode, fd as i64, pid, start, length))
    }

    pub fn ofd_getlk(
        &self, fd: u64, start: u64, length: u64,
    ) -> Result<Option<LockQuery>> {
        let entry = self.fdtable.get(fd)?;
        Ok(self.locks.ofd_getlk(entry.inode, fd as i64, start, length))
    }

    pub fn flush(&self, fd: u64) -> Result<()> {
        self.fdtable.get(fd)?;
        Ok(())
    }

    pub fn fsync(&self, fd: u64) -> Result<()> {
        self.fdtable.get(fd)?;
        if self.readonly { return Ok(()); }
        self.conn.execute_batch("PRAGMA wal_checkpoint(FULL)")?;
        Ok(())
    }

    pub fn fsyncdir(&self, path: &str) -> Result<()> {
        let ino = self.resolve_to_inode(path)?;
        let row = nodes::get(&self.conn, ino)?;
        if row.kind != NodeKind::Dir {
            return Err(Error::NotADirectory(format!("{path:?} is not a directory")));
        }
        if self.readonly { return Ok(()); }
        self.conn.execute_batch("PRAGMA wal_checkpoint(FULL)")?;
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

    // ---- tier 5a tests ----

    #[test]
    fn symlink_and_readlink_roundtrip() {
        let mut fs = fresh_fs();
        fs.symlink(b"/tmp/target", "/mylink").unwrap();
        assert_eq!(fs.readlink("/mylink").unwrap(), b"/tmp/target");
        assert_eq!(fs.stat("/mylink").unwrap().kind, NodeKind::Symlink);
    }

    #[test]
    fn readlink_on_file_is_invalid_argument() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        let err = fs.readlink("/f").unwrap_err();
        assert!(matches!(err, Error::InvalidArgument(_)));
    }

    #[test]
    fn hard_link_shares_inode_and_bumps_nlink() {
        let mut fs = fresh_fs();
        let fd = fs.create("/a", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.write_fd(fd, 0, b"shared").unwrap();
        fs.close_fd(fd).unwrap();
        fs.link("/a", "/b").unwrap();
        let sa = fs.stat("/a").unwrap();
        let sb = fs.stat("/b").unwrap();
        assert_eq!(sa.inode, sb.inode);
        assert_eq!(sa.nlink, 2);
        fs.unlink("/a").unwrap();
        let fd = fs.open("/b", libc::O_RDONLY).unwrap();
        assert_eq!(fs.read_fd(fd, 0, 6).unwrap(), b"shared");
        fs.close_fd(fd).unwrap();
    }

    #[test]
    fn hard_link_to_directory_is_rejected() {
        let mut fs = fresh_fs();
        fs.mkdir("/d", 0o755).unwrap();
        let err = fs.link("/d", "/alias").unwrap_err();
        assert!(matches!(err, Error::IsADirectory(_)));
    }

    #[test]
    fn rename_same_dir_moves_entry() {
        let mut fs = fresh_fs();
        let fd = fs.create("/a", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        fs.rename("/a", "/b").unwrap();
        assert!(matches!(fs.stat("/a"), Err(Error::NotFound(_))));
        assert_eq!(fs.stat("/b").unwrap().kind, NodeKind::File);
    }

    #[test]
    fn rename_across_dirs_moves_entry_and_adjusts_nlink() {
        let mut fs = fresh_fs();
        fs.mkdir("/src", 0o755).unwrap();
        fs.mkdir("/dst", 0o755).unwrap();
        fs.mkdir("/src/d", 0o755).unwrap();
        let src_nlink_before = fs.stat("/src").unwrap().nlink;
        let dst_nlink_before = fs.stat("/dst").unwrap().nlink;
        fs.rename("/src/d", "/dst/d").unwrap();
        assert_eq!(fs.stat("/src").unwrap().nlink, src_nlink_before - 1);
        assert_eq!(fs.stat("/dst").unwrap().nlink, dst_nlink_before + 1);
        assert_eq!(fs.stat("/dst/d").unwrap().kind, NodeKind::Dir);
    }

    #[test]
    fn rename_overwrites_existing_file() {
        let mut fs = fresh_fs();
        let fd1 = fs.create("/a", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.write_fd(fd1, 0, b"alpha").unwrap();
        fs.close_fd(fd1).unwrap();
        let fd2 = fs.create("/b", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.write_fd(fd2, 0, b"beta").unwrap();
        fs.close_fd(fd2).unwrap();
        fs.rename("/a", "/b").unwrap();
        let fd = fs.open("/b", libc::O_RDONLY).unwrap();
        assert_eq!(fs.read_fd(fd, 0, 5).unwrap(), b"alpha");
        fs.close_fd(fd).unwrap();
        assert!(matches!(fs.stat("/a"), Err(Error::NotFound(_))));
    }

    #[test]
    fn rename_into_own_subtree_rejected() {
        let mut fs = fresh_fs();
        fs.mkdir("/parent", 0o755).unwrap();
        fs.mkdir("/parent/child", 0o755).unwrap();
        let err = fs.rename("/parent", "/parent/child/nested").unwrap_err();
        assert!(matches!(err, Error::InvalidArgument(_)));
    }

    #[test]
    fn chmod_changes_mode() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        fs.chmod("/f", 0o600).unwrap();
        assert_eq!(fs.stat("/f").unwrap().mode & 0o777, 0o600);
    }

    #[test]
    fn chown_gid_by_owner_ok() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        let gid_before = fs.stat("/f").unwrap().gid;
        fs.chown("/f", None, Some(gid_before)).unwrap();
    }

    #[test]
    fn utimes_sets_atime_mtime() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        fs.utimes("/f", Some(1_000_000_000), Some(2_000_000_000)).unwrap();
        let s = fs.stat("/f").unwrap();
        assert_eq!(s.atime_ns, 1_000_000_000);
        assert_eq!(s.mtime_ns, 2_000_000_000);
    }

    // ---- tier 5b tests ----

    #[test]
    fn setxattr_and_getxattr_roundtrip() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        fs.setxattr("/f", "user.greeting", b"hi", crate::xattrs::XattrFlags::empty()).unwrap();
        assert_eq!(fs.getxattr("/f", "user.greeting").unwrap(), b"hi");
        assert_eq!(fs.listxattr("/f").unwrap(), vec!["user.greeting"]);
        fs.removexattr("/f", "user.greeting").unwrap();
        assert!(matches!(fs.getxattr("/f", "user.greeting"), Err(Error::NotFound(_))));
    }

    #[test]
    fn flock_and_release_via_close() {
        let mut fs = fresh_fs();
        let fd_a = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        let fd_b = fs.open("/f", libc::O_RDWR).unwrap();
        fs.flock(fd_a, FlockOp::Exclusive, false).unwrap();
        let err = fs.flock(fd_b, FlockOp::Exclusive, false).unwrap_err();
        assert!(matches!(err, Error::LockConflict(_)));
        fs.close_fd(fd_a).unwrap();
        fs.flock(fd_b, FlockOp::Exclusive, false).unwrap();
        fs.close_fd(fd_b).unwrap();
    }

    #[test]
    fn posix_lock_same_pid_does_not_conflict() {
        let mut fs = fresh_fs();
        let fd1 = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        let fd2 = fs.open("/f", libc::O_RDWR).unwrap();
        fs.posix_lock(fd1, 1234, LockOp::Exclusive, 0, 0, false).unwrap();
        fs.posix_lock(fd2, 1234, LockOp::Exclusive, 0, 0, false).unwrap();
        fs.close_fd(fd1).unwrap();
        fs.close_fd(fd2).unwrap();
    }

    #[test]
    fn watch_receives_mkdir_event() {
        let mut fs = fresh_fs();
        let w = fs.watch("/", false).unwrap();
        fs.mkdir("/newdir", 0o755).unwrap();
        let ev = w.try_recv().expect("mkdir should fire an event");
        assert_eq!(ev.kind, EventKind::Create);
        assert_eq!(ev.path, "/newdir");
        assert_eq!(ev.node_kind, NodeKind::Dir);
    }

    #[test]
    fn watch_non_recursive_ignores_grandchildren() {
        let mut fs = fresh_fs();
        fs.mkdir("/outer", 0o755).unwrap();
        let w = fs.watch("/outer", false).unwrap();
        fs.mkdir("/outer/inner", 0o755).unwrap();
        fs.mkdir("/outer/inner/deep", 0o755).unwrap();
        let got = w.try_recv().unwrap();
        assert_eq!(got.path, "/outer/inner");
        assert!(w.try_recv().is_none(), "grandchild should not be emitted");
    }

    #[test]
    fn watch_rename_fires_move_event() {
        let mut fs = fresh_fs();
        let fd = fs.create("/a", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        let w = fs.watch("/", false).unwrap();
        fs.rename("/a", "/b").unwrap();
        let ev = w.try_recv().expect("rename should fire Move");
        assert_eq!(ev.kind, EventKind::Move);
        assert_eq!(ev.src_path.as_deref(), Some("/a"));
        assert_eq!(ev.dst_path.as_deref(), Some("/b"));
    }

    // ---- tier 5c tests ----

    #[test]
    fn flush_and_fsync_on_valid_fd() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.write_fd(fd, 0, b"data").unwrap();
        fs.flush(fd).unwrap();
        fs.fsync(fd).unwrap();
        fs.close_fd(fd).unwrap();
    }

    #[test]
    fn fsync_on_invalid_fd_errors() {
        let fs = fresh_fs();
        assert!(matches!(fs.fsync(99999), Err(Error::BadFileDescriptor(_))));
    }

    #[test]
    fn fsyncdir_requires_directory() {
        let mut fs = fresh_fs();
        let fd = fs.create("/f", libc::O_CREAT | libc::O_RDWR, 0o644).unwrap();
        fs.close_fd(fd).unwrap();
        assert!(matches!(fs.fsyncdir("/f"), Err(Error::NotADirectory(_))));
        fs.fsyncdir("/").unwrap();
    }

    #[test]
    fn as_user_switches_and_restores_identity() {
        let mut fs = fresh_fs();
        let real_uid = fs.caller_uid;
        let real_gid = fs.caller_gid;
        {
            let mut guard = fs.as_user(4242, 4242);
            assert_eq!(guard.caller_uid, 4242);
            let err = guard.mkdir("/as_other_user", 0o755).unwrap_err();
            assert!(matches!(err, Error::PermissionDenied(_)));
        }
        assert_eq!(fs.caller_uid, real_uid);
        assert_eq!(fs.caller_gid, real_gid);
        fs.mkdir("/as_real_user", 0o755).unwrap();
    }
}
