use std::ffi::OsStr;
use std::path::Path;
use std::time::{Duration, SystemTime};

use fuser::{
    FileAttr, FileType, Filesystem as FuserFilesystem, MountOption,
    ReplyAttr, ReplyCreate, ReplyData, ReplyDirectory, ReplyEmpty, ReplyEntry,
    ReplyOpen, ReplyStatfs, ReplyWrite, ReplyXattr, Request, TimeOrNow,
};

use crate::errors::Error;
use crate::types::NodeKind;
use crate::{Filesystem, Result};

const TTL: Duration = Duration::from_secs(0);

pub struct Adapter {
    fs: Filesystem,
}

impl Adapter {
    pub fn new(fs: Filesystem) -> Self { Self { fs } }

    fn kind_to_ft(kind: NodeKind) -> FileType {
        match kind {
            NodeKind::File    => FileType::RegularFile,
            NodeKind::Dir     => FileType::Directory,
            NodeKind::Symlink => FileType::Symlink,
        }
    }

    fn stat_to_attr(st: &crate::types::Stat) -> FileAttr {
        let to_ts = |ns: i64| {
            let secs  = (ns / 1_000_000_000).max(0) as u64;
            let nanos = (ns.rem_euclid(1_000_000_000)) as u32;
            SystemTime::UNIX_EPOCH + Duration::new(secs, nanos)
        };
        FileAttr {
            ino:     st.inode,
            size:    st.size,
            blocks:  (st.size + 511) / 512,
            atime:   to_ts(st.atime_ns),
            mtime:   to_ts(st.mtime_ns),
            ctime:   to_ts(st.ctime_ns),
            crtime:  to_ts(st.ctime_ns),
            kind:    Self::kind_to_ft(st.kind),
            perm:    (st.mode & 0o7777) as u16,
            nlink:   st.nlink,
            uid:     st.uid,
            gid:     st.gid,
            rdev:    0,
            blksize: 4096,
            flags:   0,
        }
    }

    fn child_path(parent: &str, name: &str) -> String {
        if parent == "/" { format!("/{name}") } else { format!("{parent}/{name}") }
    }
}

impl FuserFilesystem for Adapter {
    fn lookup(&mut self, req: &Request, parent: u64, name: &OsStr, reply: ReplyEntry) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(parent_path) = self.fs.path_of_inode(parent) else {
            return reply.error(libc::ENOENT);
        };
        let path = Self::child_path(&parent_path, name);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.stat(&path) {
            Ok(st) => reply.entry(&TTL, &Self::stat_to_attr(&st), 0),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn getattr(&mut self, req: &Request, ino: u64, reply: ReplyAttr) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.stat(&path) {
            Ok(st) => reply.attr(&TTL, &Self::stat_to_attr(&st)),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn setattr(
        &mut self, req: &Request, ino: u64,
        mode: Option<u32>, uid: Option<u32>, gid: Option<u32>,
        size: Option<u64>,
        atime: Option<TimeOrNow>, mtime: Option<TimeOrNow>, _ctime: Option<SystemTime>,
        _fh: Option<u64>, _crtime: Option<SystemTime>, _chgtime: Option<SystemTime>,
        _bkuptime: Option<SystemTime>, _flags: Option<u32>,
        reply: ReplyAttr,
    ) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        let mut apply_result: Result<()> = Ok(());
        if let Some(m) = mode { if apply_result.is_ok() { apply_result = guard.chmod(&path, m); } }
        if uid.is_some() || gid.is_some() {
            if apply_result.is_ok() { apply_result = guard.chown(&path, uid, gid); }
        }
        if atime.is_some() || mtime.is_some() {
            let to_ns = |t: TimeOrNow| -> i64 {
                match t {
                    TimeOrNow::SpecificTime(s) => s.duration_since(SystemTime::UNIX_EPOCH)
                        .map(|d| d.as_nanos() as i64).unwrap_or(0),
                    TimeOrNow::Now => crate::watch::now_ns(),
                }
            };
            if apply_result.is_ok() {
                apply_result = guard.utimes(&path, atime.map(to_ns), mtime.map(to_ns));
            }
        }
        if let Some(new_size) = size {
            if apply_result.is_ok() {
                // Size-change via open(O_TRUNC) — zero-only truncation in v1.
                // Partial truncation (new_size > 0 and < current) needs a
                // Filesystem::truncate primitive which is a tier-7 follow-up.
                if new_size == 0 {
                    match guard.open(&path, libc::O_RDWR | libc::O_TRUNC) {
                        Ok(fd) => { let _ = guard.close_fd(fd); }
                        Err(e) => apply_result = Err(e),
                    }
                } else {
                    apply_result = Err(Error::InvalidArgument(
                        "non-zero truncate not supported in tier-6 FUSE adapter".into()
                    ));
                }
            }
        }
        let final_result = apply_result.and_then(|_| guard.stat(&path));
        match final_result {
            Ok(st) => reply.attr(&TTL, &Self::stat_to_attr(&st)),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn mkdir(
        &mut self, req: &Request, parent: u64, name: &OsStr, mode: u32,
        _umask: u32, reply: ReplyEntry,
    ) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(parent_path) = self.fs.path_of_inode(parent) else { return reply.error(libc::ENOENT); };
        let path = Self::child_path(&parent_path, name);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        let result = guard.mkdir(&path, mode).and_then(|_| guard.stat(&path));
        match result {
            Ok(st) => reply.entry(&TTL, &Self::stat_to_attr(&st), 0),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn rmdir(&mut self, req: &Request, parent: u64, name: &OsStr, reply: ReplyEmpty) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(parent_path) = self.fs.path_of_inode(parent) else { return reply.error(libc::ENOENT); };
        let path = Self::child_path(&parent_path, name);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.rmdir(&path) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn unlink(&mut self, req: &Request, parent: u64, name: &OsStr, reply: ReplyEmpty) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(parent_path) = self.fs.path_of_inode(parent) else { return reply.error(libc::ENOENT); };
        let path = Self::child_path(&parent_path, name);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.unlink(&path) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn open(&mut self, req: &Request, ino: u64, flags: i32, reply: ReplyOpen) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.open(&path, flags) {
            Ok(fd) => reply.opened(fd, 0),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn create(
        &mut self, req: &Request, parent: u64, name: &OsStr, mode: u32,
        _umask: u32, flags: i32, reply: ReplyCreate,
    ) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(parent_path) = self.fs.path_of_inode(parent) else { return reply.error(libc::ENOENT); };
        let path = Self::child_path(&parent_path, name);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        let result = guard.create(&path, flags, mode).and_then(|fd| {
            let st = guard.stat(&path)?;
            Ok((fd, st))
        });
        match result {
            Ok((fd, st)) => reply.created(&TTL, &Self::stat_to_attr(&st), 0, fd, 0),
            Err(e)       => reply.error(e.errno()),
        }
    }

    fn read(
        &mut self, req: &Request, _ino: u64, fh: u64, offset: i64, size: u32,
        _flags: i32, _lock_owner: Option<u64>, reply: ReplyData,
    ) {
        let guard = self.fs.as_user(req.uid(), req.gid());
        match guard.read_fd(fh, offset.max(0) as u64, size as u64) {
            Ok(data) => reply.data(&data),
            Err(e)   => reply.error(e.errno()),
        }
    }

    fn write(
        &mut self, req: &Request, _ino: u64, fh: u64, offset: i64, data: &[u8],
        _write_flags: u32, _flags: i32, _lock_owner: Option<u64>,
        reply: ReplyWrite,
    ) {
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.write_fd(fh, offset.max(0) as u64, data) {
            Ok(_new_size) => reply.written(data.len() as u32),
            Err(e)        => reply.error(e.errno()),
        }
    }

    fn flush(
        &mut self, _req: &Request, _ino: u64, fh: u64, _lock_owner: u64,
        reply: ReplyEmpty,
    ) {
        match self.fs.flush(fh) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn release(
        &mut self, req: &Request, _ino: u64, fh: u64, _flags: i32,
        _lock_owner: Option<u64>, _flush: bool, reply: ReplyEmpty,
    ) {
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.close_fd(fh) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn fsync(
        &mut self, _req: &Request, _ino: u64, fh: u64, _datasync: bool,
        reply: ReplyEmpty,
    ) {
        match self.fs.fsync(fh) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn opendir(&mut self, _req: &Request, _ino: u64, _flags: i32, reply: ReplyOpen) {
        reply.opened(0, 0);
    }

    fn readdir(
        &mut self, req: &Request, ino: u64, _fh: u64, offset: i64,
        mut reply: ReplyDirectory,
    ) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let guard = self.fs.as_user(req.uid(), req.gid());
        let entries = match guard.readdir(&path) {
            Ok(e)  => e,
            Err(e) => return reply.error(e.errno()),
        };

        let mut full: Vec<(u64, FileType, String)> =
            vec![(ino, FileType::Directory, ".".into())];
        if ino != crate::ROOT_INODE {
            if let Some(ppath) = parent_of_path(&path) {
                if let Ok(parent_stat) = guard.stat(ppath) {
                    full.push((parent_stat.inode, FileType::Directory, "..".into()));
                }
            }
        } else {
            full.push((ino, FileType::Directory, "..".into()));
        }
        for de in entries {
            full.push((de.inode, Self::kind_to_ft(de.kind), de.name));
        }

        for (i, (inode, kind, name)) in full.into_iter().enumerate().skip(offset as usize) {
            if reply.add(inode, (i + 1) as i64, kind, name) { break; }
        }
        reply.ok();
    }

    fn releasedir(
        &mut self, _req: &Request, _ino: u64, _fh: u64, _flags: i32, reply: ReplyEmpty,
    ) {
        reply.ok();
    }

    fn fsyncdir(
        &mut self, _req: &Request, ino: u64, _fh: u64, _datasync: bool, reply: ReplyEmpty,
    ) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        match self.fs.fsyncdir(&path) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn statfs(&mut self, _req: &Request, _ino: u64, reply: ReplyStatfs) {
        reply.statfs(
            1_000_000, 500_000, 500_000,
            1_000_000, 500_000,
            4096, 255, 4096,
        );
    }

    fn readlink(&mut self, req: &Request, ino: u64, reply: ReplyData) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let guard = self.fs.as_user(req.uid(), req.gid());
        match guard.readlink(&path) {
            Ok(target) => reply.data(&target),
            Err(e)     => reply.error(e.errno()),
        }
    }

    fn symlink(
        &mut self, req: &Request, parent: u64, name: &OsStr, target: &Path,
        reply: ReplyEntry,
    ) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(parent_path) = self.fs.path_of_inode(parent) else { return reply.error(libc::ENOENT); };
        let path = Self::child_path(&parent_path, name);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        let target_bytes = target.as_os_str().as_encoded_bytes();
        let result = guard.symlink(target_bytes, &path).and_then(|_| guard.stat(&path));
        match result {
            Ok(st) => reply.entry(&TTL, &Self::stat_to_attr(&st), 0),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn link(
        &mut self, req: &Request, ino: u64, newparent: u64, newname: &OsStr,
        reply: ReplyEntry,
    ) {
        let Some(newname) = newname.to_str() else { return reply.error(libc::EINVAL); };
        let Some(target_path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let Some(newparent_path) = self.fs.path_of_inode(newparent) else { return reply.error(libc::ENOENT); };
        let new_path = Self::child_path(&newparent_path, newname);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        let result = guard.link(&target_path, &new_path).and_then(|_| guard.stat(&new_path));
        match result {
            Ok(st) => reply.entry(&TTL, &Self::stat_to_attr(&st), 0),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn rename(
        &mut self, req: &Request, parent: u64, name: &OsStr,
        newparent: u64, newname: &OsStr, _flags: u32, reply: ReplyEmpty,
    ) {
        let (Some(name), Some(newname)) = (name.to_str(), newname.to_str()) else {
            return reply.error(libc::EINVAL);
        };
        let Some(parent_path) = self.fs.path_of_inode(parent) else { return reply.error(libc::ENOENT); };
        let Some(newparent_path) = self.fs.path_of_inode(newparent) else { return reply.error(libc::ENOENT); };
        let src = Self::child_path(&parent_path, name);
        let dst = Self::child_path(&newparent_path, newname);
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.rename(&src, &dst) {
            Ok(())  => reply.ok(),
            Err(e)  => reply.error(e.errno()),
        }
    }

    fn access(&mut self, req: &Request, ino: u64, mask: i32, reply: ReplyEmpty) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let guard = self.fs.as_user(req.uid(), req.gid());
        let row = match guard.stat(&path) {
            Ok(s)  => s,
            Err(e) => return reply.error(e.errno()),
        };
        let access = crate::types::Access::from_bits_truncate(mask as u32);
        match crate::perms::require_access(
            row.mode, row.uid, row.gid,
            req.uid(), req.gid(), access,
        ) {
            Ok(())  => reply.ok(),
            Err(e)  => reply.error(e.errno()),
        }
    }

    fn setxattr(
        &mut self, req: &Request, ino: u64, name: &OsStr, value: &[u8],
        _flags: i32, _position: u32, reply: ReplyEmpty,
    ) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        let flags = crate::xattrs::XattrFlags::empty();
        match guard.setxattr(&path, name, value, flags) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }

    fn getxattr(
        &mut self, req: &Request, ino: u64, name: &OsStr, size: u32, reply: ReplyXattr,
    ) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let guard = self.fs.as_user(req.uid(), req.gid());
        match guard.getxattr(&path, name) {
            Ok(v) => {
                if size == 0 { reply.size(v.len() as u32); }
                else if (v.len() as u32) <= size { reply.data(&v); }
                else { reply.error(libc::ERANGE); }
            }
            Err(e) => reply.error(e.errno()),
        }
    }

    fn listxattr(&mut self, req: &Request, ino: u64, size: u32, reply: ReplyXattr) {
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let guard = self.fs.as_user(req.uid(), req.gid());
        match guard.listxattr(&path) {
            Ok(names) => {
                let mut buf = Vec::new();
                for n in &names { buf.extend_from_slice(n.as_bytes()); buf.push(0); }
                if size == 0 { reply.size(buf.len() as u32); }
                else if (buf.len() as u32) <= size { reply.data(&buf); }
                else { reply.error(libc::ERANGE); }
            }
            Err(e) => reply.error(e.errno()),
        }
    }

    fn removexattr(&mut self, req: &Request, ino: u64, name: &OsStr, reply: ReplyEmpty) {
        let Some(name) = name.to_str() else { return reply.error(libc::EINVAL); };
        let Some(path) = self.fs.path_of_inode(ino) else { return reply.error(libc::ENOENT); };
        let mut guard = self.fs.as_user(req.uid(), req.gid());
        match guard.removexattr(&path, name) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e.errno()),
        }
    }
}

fn parent_of_path(path: &str) -> Option<&str> {
    if path == "/" { return None; }
    match path.rfind('/') {
        Some(0)   => Some("/"),
        Some(idx) => Some(&path[..idx]),
        None      => None,
    }
}

pub struct MountOptions {
    pub readonly:            bool,
    pub sync_mode:           crate::schema::SyncMode,
    pub checkpoint_interval: Option<Duration>,
}

impl Default for MountOptions {
    fn default() -> Self {
        Self {
            readonly: false,
            sync_mode: crate::schema::SyncMode::default(),
            checkpoint_interval: None,
        }
    }
}

pub fn mount(db: &Path, mountpoint: &Path, opts: MountOptions) -> Result<()> {
    let fs = crate::open_fs(db, crate::OpenOptions {
        readonly: opts.readonly,
        sync_mode: opts.sync_mode,
        checkpoint_interval: opts.checkpoint_interval,
        ..Default::default()
    })?;
    let adapter = Adapter::new(fs);

    let mount_opts = vec![
        MountOption::FSName("sqlite-fs".into()),
        MountOption::DefaultPermissions,
    ];
    fuser::mount2(adapter, mountpoint, &mount_opts)
        .map_err(|e| Error::InvalidArgument(format!("fuser mount failed: {e}")))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adapter_is_fuser_filesystem() {
        fn assert_impl<T: FuserFilesystem>() {}
        assert_impl::<Adapter>();
    }
}
