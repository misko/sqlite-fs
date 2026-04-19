//! sqlite-fs — durable FUSE filesystem backed by SQLite.
//!
//! Engspec-driven Rust port. This file currently exposes only the
//! pure-function modules that have been implemented (Tier 1 of the port);
//! storage, orchestrator, and FUSE adapter modules will be added as they
//! land.

#![allow(dead_code)]

pub mod errors;
pub mod types;
pub mod paths;
pub mod schema;
pub mod nodes;
pub mod entries;
pub mod blobs;
pub mod symlinks;
pub mod xattrs;
pub mod perms;
pub mod fdtable;
pub mod locks;
pub mod fsck;
pub mod watch;
pub mod fs;
pub mod mkfs;

#[cfg(feature = "fuse")]
pub mod fuse;

pub use errors::{Error, Result};
pub use types::{
    Access, DirEntry, Event, EventKind, FlockOp, FsckIssue, FsckKind, FsckReport,
    IntegrityResult, LockOp, LockQuery, LockType, NodeKind, Stat,
};
pub use paths::{parse_path, NAME_MAX, PATH_MAX};
pub use schema::{SyncMode, DDL, DEFAULT_CHUNK_SIZE, MAXSYMLINKS, ROOT_INODE, SCHEMA_VERSION};
pub use watch::{Watcher, WatchMask};
pub use fs::Filesystem;
pub use mkfs::{mkfs, open_fs, MkfsOptions, OpenOptions};
