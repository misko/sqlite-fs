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

pub use errors::{Error, Result};
pub use types::{
    Access, DirEntry, Event, EventKind, FlockOp, FsckIssue, FsckKind, FsckReport,
    IntegrityResult, LockOp, LockQuery, LockType, NodeKind, Stat,
};
pub use paths::{parse_path, NAME_MAX, PATH_MAX};
