use std::collections::HashMap;

use crate::errors::{Error, Result};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FdEntry {
    pub fd:     u64,
    pub inode:  u64,
    pub flags:  i32,
    pub uid:    u32,
    pub gid:    u32,
    pub offset: u64,
}

pub struct FdTable {
    entries: HashMap<u64, FdEntry>,
    next_fd: u64,
}

impl FdTable {
    pub fn new() -> Self {
        Self { entries: HashMap::new(), next_fd: 1 }
    }

    pub fn open(&mut self, inode: u64, flags: i32, uid: u32, gid: u32) -> u64 {
        let fd = self.next_fd;
        self.next_fd += 1;
        self.entries.insert(fd, FdEntry { fd, inode, flags, uid, gid, offset: 0 });
        fd
    }

    pub fn get(&self, fd: u64) -> Result<&FdEntry> {
        self.entries.get(&fd)
            .ok_or_else(|| Error::BadFileDescriptor(format!("unknown fd {fd}")))
    }

    pub fn get_mut(&mut self, fd: u64) -> Result<&mut FdEntry> {
        self.entries.get_mut(&fd)
            .ok_or_else(|| Error::BadFileDescriptor(format!("unknown fd {fd}")))
    }

    pub fn close(&mut self, fd: u64) -> Result<u64> {
        self.entries.remove(&fd)
            .map(|e| e.inode)
            .ok_or_else(|| Error::BadFileDescriptor(format!("unknown fd {fd}")))
    }

    pub fn open_count(&self, inode: u64) -> usize {
        self.entries.values().filter(|e| e.inode == inode).count()
    }

    pub fn fds_for_inode(&self, inode: u64) -> Vec<u64> {
        self.entries.values().filter(|e| e.inode == inode).map(|e| e.fd).collect()
    }
}

impl Default for FdTable {
    fn default() -> Self { Self::new() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_table_has_no_entries() {
        let t = FdTable::new();
        assert_eq!(t.open_count(1), 0);
        assert!(t.fds_for_inode(1).is_empty());
    }

    #[test]
    fn open_assigns_monotonic_fds() {
        let mut t = FdTable::new();
        let a = t.open(1, 0, 1000, 1000);
        let b = t.open(1, 0, 1000, 1000);
        assert!(b > a);
    }

    #[test]
    fn get_returns_entry_after_open() {
        let mut t = FdTable::new();
        let fd = t.open(42, 0o2, 1000, 1000);
        let e = t.get(fd).unwrap();
        assert_eq!(e.inode, 42);
        assert_eq!(e.flags, 0o2);
        assert_eq!(e.uid, 1000);
    }

    #[test]
    fn close_returns_inode_and_removes_entry() {
        let mut t = FdTable::new();
        let fd = t.open(7, 0, 1000, 1000);
        assert_eq!(t.close(fd).unwrap(), 7);
        assert!(matches!(t.get(fd), Err(Error::BadFileDescriptor(_))));
    }

    #[test]
    fn close_twice_is_bad_file_descriptor() {
        let mut t = FdTable::new();
        let fd = t.open(7, 0, 1000, 1000);
        t.close(fd).unwrap();
        assert!(matches!(t.close(fd), Err(Error::BadFileDescriptor(_))));
    }

    #[test]
    fn open_count_tracks_same_inode() {
        let mut t = FdTable::new();
        let fd_a = t.open(9, 0, 1000, 1000);
        let _fd_b = t.open(9, 0, 1000, 1000);
        let _fd_c = t.open(9, 0, 1000, 1000);
        assert_eq!(t.open_count(9), 3);
        assert_eq!(t.fds_for_inode(9).len(), 3);
        t.close(fd_a).unwrap();
        assert_eq!(t.open_count(9), 2);
    }
}
