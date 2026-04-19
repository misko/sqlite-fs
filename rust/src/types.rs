use bitflags::bitflags;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NodeKind { File, Dir, Symlink }

impl NodeKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            NodeKind::File => "file",
            NodeKind::Dir => "dir",
            NodeKind::Symlink => "symlink",
        }
    }
    pub fn from_str(s: &str) -> Option<NodeKind> {
        match s {
            "file"    => Some(NodeKind::File),
            "dir"     => Some(NodeKind::Dir),
            "symlink" => Some(NodeKind::Symlink),
            _         => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockOp { Shared, Exclusive, Unlock }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlockOp { Shared, Exclusive, Unlock }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockType { Shared, Exclusive }

bitflags! {
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct Access: u32 {
        const R = 4;
        const W = 2;
        const X = 1;
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stat {
    pub kind:      NodeKind,
    pub size:      u64,
    pub mode:      u32,
    pub uid:       u32,
    pub gid:       u32,
    pub atime_ns:  i64,
    pub mtime_ns:  i64,
    pub ctime_ns:  i64,
    pub nlink:     u32,
    pub inode:     u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirEntry {
    pub name:  String,
    pub kind:  NodeKind,
    pub inode: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockQuery {
    pub type_:  LockType,
    pub pid:    i64,
    pub start:  i64,
    pub length: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FsckKind {
    OrphanBlob,
    OrphanXattr,
    OrphanSymlink,
    Cycle,
    NlinkMismatch,
    DanglingParent,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FsckIssue {
    pub kind:   FsckKind,
    pub inode:  Option<u64>,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FsckReport {
    pub integrity_check_result: IntegrityResult,
    pub issues: Vec<FsckIssue>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntegrityResult { Ok, Corrupted }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventKind { Create, Remove, Modify, Move, Metadata }

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub kind:         EventKind,
    pub path:         String,
    pub src_path:     Option<String>,
    pub dst_path:     Option<String>,
    pub node_kind:    NodeKind,
    pub inode:        u64,
    pub timestamp_ns: i64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stat_is_field_equal() {
        let a = Stat {
            kind: NodeKind::File, size: 5, mode: 0o644,
            uid: 1000, gid: 1000, atime_ns: 1, mtime_ns: 1,
            ctime_ns: 1, nlink: 1, inode: 42,
        };
        let b = a.clone();
        assert_eq!(a, b);
    }

    #[test]
    fn access_flags_compose() {
        let rw = Access::R | Access::W;
        assert!(rw.contains(Access::R));
        assert!(rw.contains(Access::W));
        assert!(!rw.contains(Access::X));
        assert_eq!(rw.bits(), 6);
    }

    #[test]
    fn nodekind_roundtrips_as_str() {
        for k in [NodeKind::File, NodeKind::Dir, NodeKind::Symlink] {
            assert_eq!(NodeKind::from_str(k.as_str()), Some(k));
        }
        assert_eq!(NodeKind::from_str("bogus"), None);
    }
}
