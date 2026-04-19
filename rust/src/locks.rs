use std::collections::HashMap;

use crate::errors::{Error, Result};
use crate::types::{FlockOp, LockOp, LockQuery, LockType};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Namespace { Posix, Ofd, Flock }

#[derive(Debug, Clone)]
struct Record {
    namespace: Namespace,
    type_:     LockType,
    owner:     i64,
    start:     u64,
    length:    u64,
}

fn end(rec: &Record) -> Option<u64> {
    if rec.length == 0 { None } else { Some(rec.start + rec.length) }
}

fn overlaps(a_start: u64, a_end: Option<u64>, b_start: u64, b_end: Option<u64>) -> bool {
    let no_overlap = match (a_end, b_end) {
        (Some(ae), _) if ae <= b_start => true,
        (_, Some(be)) if be <= a_start => true,
        _ => false,
    };
    !no_overlap
}

fn rec_overlaps(rec: &Record, start: u64, length: u64) -> bool {
    let b_end = if length == 0 { None } else { Some(start + length) };
    overlaps(rec.start, end(rec), start, b_end)
}

pub struct LockManager {
    by_inode: HashMap<u64, Vec<Record>>,
}

impl LockManager {
    pub fn new() -> Self { Self { by_inode: HashMap::new() } }

    pub fn posix_lock(
        &mut self, inode: u64, _fd_id: i64, pid: i64,
        op: LockOp, start: u64, length: u64, _wait: bool,
    ) -> Result<()> {
        self.namespace_lock(
            Namespace::Posix, inode, pid, op, start, length,
            move |r| r.namespace == Namespace::Posix && r.owner != pid,
        )
    }

    pub fn ofd_lock(
        &mut self, inode: u64, fd_id: i64,
        op: LockOp, start: u64, length: u64, _wait: bool,
    ) -> Result<()> {
        self.namespace_lock(
            Namespace::Ofd, inode, fd_id, op, start, length,
            move |r| r.namespace == Namespace::Ofd && r.owner != fd_id,
        )
    }

    pub fn flock(
        &mut self, inode: u64, fd_id: i64, op: FlockOp, _wait: bool,
    ) -> Result<()> {
        let lock_op = match op {
            FlockOp::Shared    => LockOp::Shared,
            FlockOp::Exclusive => LockOp::Exclusive,
            FlockOp::Unlock    => LockOp::Unlock,
        };
        self.namespace_lock(
            Namespace::Flock, inode, fd_id, lock_op, 0, 0,
            move |r| r.namespace == Namespace::Flock && r.owner != fd_id,
        )
    }

    fn namespace_lock(
        &mut self, namespace: Namespace, inode: u64, owner: i64,
        op: LockOp, start: u64, length: u64,
        is_conflict_candidate: impl Fn(&Record) -> bool,
    ) -> Result<()> {
        let records = self.by_inode.entry(inode).or_default();

        match op {
            LockOp::Unlock => {
                let mut out: Vec<Record> = Vec::with_capacity(records.len());
                for r in records.drain(..) {
                    if r.namespace != namespace || r.owner != owner {
                        out.push(r);
                        continue;
                    }
                    if !rec_overlaps(&r, start, length) {
                        out.push(r);
                        continue;
                    }
                    let unlock_end = if length == 0 { None } else { Some(start + length) };
                    let r_end = end(&r);
                    if r.start < start {
                        let frag_end = match r_end {
                            Some(e) => std::cmp::min(e, start),
                            None    => start,
                        };
                        out.push(Record {
                            length: frag_end - r.start,
                            ..r.clone()
                        });
                    }
                    if let Some(ue) = unlock_end {
                        let frag_start_ok = match r_end {
                            Some(e) => ue < e,
                            None    => true,
                        };
                        if frag_start_ok {
                            let new_len = match r_end {
                                Some(e) => e - ue,
                                None    => 0,
                            };
                            out.push(Record {
                                start: ue,
                                length: new_len,
                                ..r
                            });
                        }
                    }
                }
                if out.is_empty() { self.by_inode.remove(&inode); }
                else              { *self.by_inode.get_mut(&inode).unwrap() = out; }
                Ok(())
            }
            LockOp::Shared | LockOp::Exclusive => {
                let new_type = if op == LockOp::Exclusive { LockType::Exclusive } else { LockType::Shared };
                for r in records.iter() {
                    if !is_conflict_candidate(r) { continue; }
                    if !rec_overlaps(r, start, length) { continue; }
                    let conflict = r.type_ == LockType::Exclusive
                                || new_type == LockType::Exclusive;
                    if conflict {
                        return Err(Error::LockConflict(format!(
                            "conflicting {:?} lock on inode {inode} [{start}, +{length})",
                            r.type_
                        )));
                    }
                }
                let existing = records.iter_mut().find(|r| {
                    r.namespace == namespace && r.owner == owner
                        && r.start == start && r.length == length
                });
                match existing {
                    Some(r) => r.type_ = new_type,
                    None    => records.push(Record {
                        namespace, type_: new_type, owner, start, length,
                    }),
                }
                Ok(())
            }
        }
    }

    pub fn posix_getlk(
        &self, inode: u64, _fd_id: i64, pid: i64, start: u64, length: u64,
    ) -> Option<LockQuery> {
        self.getlk(Namespace::Posix, inode, start, length,
                   move |r| r.owner != pid)
    }

    pub fn ofd_getlk(
        &self, inode: u64, fd_id: i64, start: u64, length: u64,
    ) -> Option<LockQuery> {
        self.getlk(Namespace::Ofd, inode, start, length,
                   move |r| r.owner != fd_id)
    }

    fn getlk(
        &self, namespace: Namespace, inode: u64, start: u64, length: u64,
        owner_filter: impl Fn(&Record) -> bool,
    ) -> Option<LockQuery> {
        let records = self.by_inode.get(&inode)?;
        for r in records {
            if r.namespace != namespace { continue; }
            if !owner_filter(r) { continue; }
            if !rec_overlaps(r, start, length) { continue; }
            return Some(LockQuery {
                type_:  r.type_,
                pid:    r.owner,
                start:  r.start as i64,
                length: r.length as i64,
            });
        }
        None
    }

    pub fn on_fd_close(&mut self, inode: u64, fd_id: i64, pid: i64) {
        let Some(records) = self.by_inode.get_mut(&inode) else { return; };
        records.retain(|r| !match r.namespace {
            Namespace::Posix => r.owner == pid,
            Namespace::Ofd   => r.owner == fd_id,
            Namespace::Flock => r.owner == fd_id,
        });
        if records.is_empty() { self.by_inode.remove(&inode); }
    }
}

impl Default for LockManager {
    fn default() -> Self { Self::new() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn posix_exclusive_blocks_shared_from_other_pid() {
        let mut m = LockManager::new();
        m.posix_lock(1, 10, 100, LockOp::Exclusive, 0, 0, false).unwrap();
        let err = m.posix_lock(1, 11, 200, LockOp::Shared, 0, 0, false).unwrap_err();
        assert!(matches!(err, Error::LockConflict(_)));
    }

    #[test]
    fn posix_same_pid_does_not_conflict() {
        let mut m = LockManager::new();
        m.posix_lock(1, 10, 100, LockOp::Exclusive, 0, 0, false).unwrap();
        m.posix_lock(1, 11, 100, LockOp::Exclusive, 0, 0, false).unwrap();
    }

    #[test]
    fn posix_shared_stacks_across_pids() {
        let mut m = LockManager::new();
        m.posix_lock(1, 10, 100, LockOp::Shared, 0, 0, false).unwrap();
        m.posix_lock(1, 11, 200, LockOp::Shared, 0, 0, false).unwrap();
    }

    #[test]
    fn posix_and_flock_are_separate_namespaces() {
        let mut m = LockManager::new();
        m.flock(1, 10, FlockOp::Exclusive, false).unwrap();
        m.posix_lock(1, 11, 200, LockOp::Exclusive, 0, 0, false).unwrap();
    }

    #[test]
    fn posix_and_ofd_are_separate_namespaces() {
        let mut m = LockManager::new();
        m.posix_lock(1, 10, 100, LockOp::Exclusive, 0, 0, false).unwrap();
        m.ofd_lock(1, 11, LockOp::Exclusive, 0, 0, false).unwrap();
    }

    #[test]
    fn ofd_locks_contend_across_fds_same_pid() {
        let mut m = LockManager::new();
        m.ofd_lock(1, 10, LockOp::Exclusive, 0, 0, false).unwrap();
        let err = m.ofd_lock(1, 11, LockOp::Exclusive, 0, 0, false).unwrap_err();
        assert!(matches!(err, Error::LockConflict(_)));
    }

    #[test]
    fn flock_exclusive_blocks_shared() {
        let mut m = LockManager::new();
        m.flock(1, 10, FlockOp::Exclusive, false).unwrap();
        let err = m.flock(1, 11, FlockOp::Shared, false).unwrap_err();
        assert!(matches!(err, Error::LockConflict(_)));
    }

    #[test]
    fn flock_shared_stacks() {
        let mut m = LockManager::new();
        m.flock(1, 10, FlockOp::Shared, false).unwrap();
        m.flock(1, 11, FlockOp::Shared, false).unwrap();
    }

    #[test]
    fn getlk_returns_none_when_free() {
        let m = LockManager::new();
        assert!(m.posix_getlk(1, 10, 100, 0, 0).is_none());
    }

    #[test]
    fn getlk_reports_conflicting_lock() {
        let mut m = LockManager::new();
        m.posix_lock(1, 10, 100, LockOp::Exclusive, 0, 0, false).unwrap();
        let q = m.posix_getlk(1, 11, 200, 0, 0).unwrap();
        assert_eq!(q.type_, LockType::Exclusive);
        assert_eq!(q.pid, 100);
    }

    #[test]
    fn posix_close_any_fd_releases_all_pids_locks() {
        let mut m = LockManager::new();
        m.posix_lock(1, 10, 100, LockOp::Exclusive, 0, 0, false).unwrap();
        m.posix_lock(1, 11, 100, LockOp::Exclusive, 100, 200, false).unwrap();
        m.on_fd_close(1, 99, 100);
        m.posix_lock(1, 12, 200, LockOp::Exclusive, 0, 0, false).unwrap();
    }

    #[test]
    fn ofd_close_releases_only_its_locks() {
        let mut m = LockManager::new();
        m.ofd_lock(1, 10, LockOp::Exclusive, 0,   50,  false).unwrap();
        m.ofd_lock(1, 11, LockOp::Exclusive, 100, 200, false).unwrap();
        m.on_fd_close(1, 10, 100);
        let err = m.ofd_lock(1, 12, LockOp::Exclusive, 100, 200, false).unwrap_err();
        assert!(matches!(err, Error::LockConflict(_)));
        m.ofd_lock(1, 13, LockOp::Exclusive, 0, 50, false).unwrap();
    }
}
