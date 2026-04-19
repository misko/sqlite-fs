//! sqlite-fs — durable FUSE filesystem backed by SQLite.
//!
//! This crate is an in-progress Rust port of the Python reference implementation.
//! Engspecs under `package/specs/src/` define the intended surface; module bodies
//! here will be filled in from those specs.
//!
//! Public API (see `package/specs/src/lib.rs.engspec`):
//!   mkfs, open_fs, Filesystem, MkfsOptions, OpenOptions, SyncMode,
//!   plus Error / Result / Stat / NodeKind / Event.

#![allow(dead_code)]
#![allow(unused_variables)]

pub mod errors;
pub mod types;

mod paths;
mod schema;
mod nodes;
mod entries;
mod blobs;
mod symlinks;
mod xattrs;
mod fdtable;
mod locks;
mod perms;
mod fsck;
mod fs;
mod mkfs;
pub mod watch;

#[cfg(feature = "fuse")]
pub mod fuse;

pub use errors::{Error, Result};
pub use types::{Access, Event, FlockOp, LockOp, LockType, NodeKind, Stat};
pub use schema::SyncMode;
pub use mkfs::{mkfs, open_fs, MkfsOptions, OpenOptions};
pub use fs::Filesystem;
pub use watch::{Watcher, WatchMask};
