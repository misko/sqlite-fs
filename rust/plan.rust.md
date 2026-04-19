# Plan (Rust port) — design decisions

*Companion to `idea.md` and plan.md / plan.v2 … v7. Those describe the language-agnostic design. This document names the Rust-specific choices.*

## What stays language-agnostic

- `idea.md` scope (FUSE, durability, hard links, locks, xattrs, symlinks, perf targets) is unchanged.
- The 4-table schema from plan.v3 is unchanged — it's SQL.
- The three-flavor lock namespace model, hard-link GC rules, POSIX-open-on-newly-created semantic, event emission protocol, and sync-mode dial all carry over.
- 40 engspec files map 1:1 from the Python package to the Rust package. Purpose / Preconditions / Postconditions / Invariants / Failure Modes / Test Strategy stay substantially the same. Only Implementation Notes are rewritten in Rust.

## Rust-specific decisions

### Crate layout

```
rust/
├── Cargo.toml
├── README.md
├── idea.md, plan*.md, plan.rust.md       # language-agnostic + Rust-specific plan
├── src/
│   ├── lib.rs              # public API re-exports (replaces Python __init__.py)
│   ├── errors.rs           # Error enum + errno mapping
│   ├── types.rs            # Stat, DirEntry, LockQuery, FsckIssue/Report, Access
│   ├── paths.rs            # parse_path + consts
│   ├── schema.rs           # DDL, PRAGMAs, sync mode
│   ├── perms.rs
│   ├── fdtable.rs
│   ├── locks.rs
│   ├── nodes.rs            # NodeRow + SQL CRUD on nodes table
│   ├── entries.rs          # EntryRow + CRUD on entries table
│   ├── blobs.rs
│   ├── xattrs.rs
│   ├── symlinks.rs
│   ├── fsck.rs
│   ├── watch.rs
│   ├── fs.rs               # Filesystem struct
│   ├── mkfs.rs             # mkfs / open_fs entry points
│   ├── fuse/
│   │   ├── mod.rs          # replaces fuse/__init__.py
│   │   ├── adapter.rs      # fuser::Filesystem trait impl
│   │   └── cli.rs          # mount / umount helpers
│   └── bin/
│       └── sqlite-fs.rs    # CLI binary
├── tests/
│   └── ...                 # integration tests
└── package/
    └── specs/              # engspec package (mirrors Python's)
```

### Dependency choices

| Concern | Crate | Why |
|---|---|---|
| SQLite | `rusqlite` (with `bundled` feature) | mature, sync, ergonomic. Same-thread connection model matches our single-writer design. Bundled feature means we don't need a system libsqlite. |
| FUSE | `fuser` (optional feature) | the standard Rust FUSE crate, sync callbacks, simpler than async. Gated behind `--features fuse`. |
| Errors | `thiserror` | derive macro for an `Error` enum with automatic `Display` and source chaining. |
| Test locals | `tempfile` | `TempDir` for per-test directories. |
| Subprocess / SIGKILL fixture | `nix` | `nix::sys::signal::kill`, `nix::unistd::Pid`. |
| Property tests | `proptest` | Rust analogue of Python's hypothesis. plan.v2's stateful test maps to `proptest-stateful`. |

### Error handling

Python exceptions → Rust `Result<T, Error>`. Single `Error` enum:

```rust
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("path syntax: {0}")]
    PathSyntax(String),
    #[error("name too long: {0}")]
    NameTooLong(String),
    #[error("not found: {0}")]
    NotFound(String),
    #[error("already exists: {0}")]
    AlreadyExists(String),
    #[error("not a directory: {0}")]
    NotADirectory(String),
    #[error("is a directory: {0}")]
    IsADirectory(String),
    #[error("directory not empty: {0}")]
    DirectoryNotEmpty(String),
    #[error("permission denied: {0}")]
    PermissionDenied(String),
    #[error("read-only filesystem")]
    ReadOnlyFilesystem,
    #[error("invalid xattr: {0}")]
    InvalidXattr(String),
    #[error("lock conflict: {0}")]
    LockConflict(String),
    #[error("bad file descriptor: {0}")]
    BadFileDescriptor(String),
    #[error("symlink loop: {0}")]
    SymlinkLoop(String),
    #[error("invalid argument: {0}")]
    InvalidArgument(String),

    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

impl Error {
    /// Returns the POSIX errno this error maps to. The FUSE adapter uses
    /// this to translate into kernel error codes.
    pub fn errno(&self) -> i32 {
        use libc::*;
        match self {
            Error::PathSyntax(_) | Error::InvalidArgument(_) | Error::InvalidXattr(_) => EINVAL,
            Error::NotFound(_) => ENOENT,
            Error::AlreadyExists(_) => EEXIST,
            Error::NotADirectory(_) => ENOTDIR,
            Error::IsADirectory(_) => EISDIR,
            Error::DirectoryNotEmpty(_) => ENOTEMPTY,
            Error::PermissionDenied(_) => EACCES,
            Error::ReadOnlyFilesystem => EROFS,
            Error::NameTooLong(_) => ENAMETOOLONG,
            Error::LockConflict(_) => EAGAIN,
            Error::BadFileDescriptor(_) => EBADF,
            Error::SymlinkLoop(_) => ELOOP,
            Error::Sqlite(_) => EIO,
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;
```

### Ownership & shared mutable state

The Python `Filesystem` class held mutable state via `self._fd_table`, `self._lock_mgr`, `self._watchers`, `self._path_cache`. In Rust:

- `Filesystem` holds `Connection` (`rusqlite::Connection` — not `Send + Sync`), `RefCell`-wrapped or owned interior-mutable containers for `FdTable`, `LockManager`, `Watchers`, path cache. Since FUSE callbacks get `&mut self` on the Filesystem impl, we can use plain `Vec` / `HashMap` fields rather than `Arc<Mutex<...>>` — no threading in the main path.

- **Exception: the checkpoint thread** (plan.v6) needs its own `Connection` on its own thread. That connection is local to the thread; no sharing.

- **`as_user` context manager** becomes a guard pattern. `Filesystem::as_user(uid, gid)` returns a `UserGuard<'_>` that restores identity on Drop. Internally it's an identity-restore closure managed by RAII rather than `__enter__`/`__exit__`.

### Public API shape

```rust
pub struct Filesystem { /* ... */ }

impl Filesystem {
    pub fn mkdir(&mut self, path: &str, mode: u32) -> Result<()>;
    pub fn rmdir(&mut self, path: &str) -> Result<()>;
    pub fn readdir(&mut self, path: &str) -> Result<Vec<DirEntry>>;
    pub fn create(&mut self, path: &str, mode: u32, flags: i32) -> Result<Fd>;
    pub fn open(&mut self, path: &str, flags: i32, mode: u32) -> Result<Fd>;
    pub fn read(&mut self, fd: Fd, size: usize, offset: u64) -> Result<Vec<u8>>;
    pub fn write(&mut self, fd: Fd, data: &[u8], offset: u64) -> Result<usize>;
    pub fn close_fd(&mut self, fd: Fd) -> Result<()>;
    pub fn stat(&mut self, path: &str, follow_symlinks: bool) -> Result<Stat>;
    pub fn symlink(&mut self, target: &[u8], linkpath: &str) -> Result<()>;
    pub fn readlink(&mut self, path: &str) -> Result<Vec<u8>>;
    pub fn link(&mut self, src: &str, dst: &str) -> Result<()>;
    pub fn rename(&mut self, src: &str, dst: &str, noreplace: bool, exchange: bool) -> Result<()>;
    pub fn unlink(&mut self, path: &str) -> Result<()>;
    pub fn chmod(&mut self, path: &str, mode: u32, follow_symlinks: bool) -> Result<()>;
    pub fn chown(&mut self, path: &str, uid: u32, gid: u32, follow_symlinks: bool) -> Result<()>;
    pub fn utimes(&mut self, path: &str, atime_ns: i64, mtime_ns: i64, follow_symlinks: bool) -> Result<()>;
    pub fn getxattr(&mut self, path: &str, name: &str) -> Result<Vec<u8>>;
    pub fn setxattr(&mut self, path: &str, name: &str, value: &[u8], flags: i32) -> Result<()>;
    pub fn listxattr(&mut self, path: &str) -> Result<Vec<String>>;
    pub fn removexattr(&mut self, path: &str, name: &str) -> Result<()>;
    pub fn fsync(&mut self, fd: Fd, datasync: bool) -> Result<()>;
    pub fn truncate(&mut self, path: &str, size: u64) -> Result<()>;
    pub fn truncate_fd(&mut self, fd: Fd, size: u64) -> Result<()>;
    pub fn exists(&mut self, path: &str) -> bool;
    pub fn fsck(&mut self) -> Result<FsckReport>;
    pub fn watch(&mut self, path: &str, recursive: bool) -> Result<Watcher>;
    pub fn as_user(&mut self, uid: u32, gid: u32) -> UserGuard<'_>;
}
```

Entry points:

```rust
pub fn mkfs(path: &str, options: MkfsOptions) -> Result<()>;
pub fn open_fs(path: &str, options: OpenOptions) -> Result<Filesystem>;

pub struct OpenOptions {
    pub readonly: bool,
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    pub sync_mode: SyncMode,             // default Normal (plan.v7)
    pub checkpoint_interval: Option<Duration>,   // plan.v6
}
```

### FUSE integration

`fuser::Filesystem` trait:

```rust
impl fuser::Filesystem for Adapter {
    fn lookup(&mut self, req: &Request, parent: u64, name: &OsStr, reply: ReplyEntry) { ... }
    fn getattr(&mut self, req: &Request, ino: u64, reply: ReplyAttr) { ... }
    fn mkdir(&mut self, ...) { ... }
    // 30-ish callbacks
}
```

`fuser::mount2(adapter, mountpoint, &options)` blocks until unmount. Run it on the main thread; the library's mutable state is owned by the adapter so no shared state.

### Error translation

FUSE `reply.error(errno)` instead of `pyfuse3.FUSEError(errno)`. Same mechanism: library errors have an `errno()` method; the adapter catches and dispatches.

### Async: not needed

`fuser` is sync; `rusqlite` is sync. The daemon is single-threaded except for the plan.v6 checkpoint thread. No tokio, no async/await.

### Testing

- `cargo test` runs all tests in `tests/` directory.
- Per-module unit tests inline in `src/foo.rs` via `#[cfg(test)] mod tests`.
- Integration tests (like the Python `tests/test_fs.py`) live in `tests/` as separate test binaries.
- `proptest` for the plan.v2 stateful hypothesis test.
- FUSE mount tests gated behind `--features fuse`.
- SIGKILL / crash tests use `nix` crate.

### What ports 1:1

- Every `.engspec` file — Purpose / Pre / Post / Invariants / Failure Modes stay. Only Implementation Notes change.
- All 6 plan.vN.md findings apply to Rust too. The 7th (plan.v7 default sync_mode) is baked into `OpenOptions::default()`.

### What changes materially

- No `__init__.py` → becomes `lib.rs` with `pub use` re-exports.
- Context managers → RAII guards via Drop.
- `sqlite3.Connection` → `rusqlite::Connection`.
- Python `bytes` ↔ Rust `Vec<u8>` / `&[u8]`.
- Python `dict` ↔ `HashMap` / `BTreeMap`.
- Python `set` ↔ `HashSet` / `BTreeSet`.
- Exception classes → single `Error` enum.
- `self._xxx` → struct fields.

## Iteration plan

1. Cargo.toml + plan.rust.md + README.md (this commit).
2. Critical engspecs updated: `errors`, `types`, `paths`, `schema`, `mkfs`, `lib` (6 files).
3. Storage engspecs: `nodes`, `entries`, `blobs`, `xattrs`, `symlinks`, `fdtable`, `locks`, `perms` (8 files).
4. Orchestration engspecs: `fs`, `fsck`, `watch` (3 files).
5. FUSE engspecs: `fuse/mod`, `fuse/adapter`, `fuse/cli` (3 files).
6. CLI engspec: `cli` → `bin/sqlite-fs.rs` (1 file).
7. Test engspecs: update imports/framework, keep assertion semantics.
8. Write `src/errors.rs`, `src/types.rs`, `src/paths.rs` — smallest modules, build first.
9. Add integration test with `cargo test`, confirm it runs.
10. Continue with nodes/blobs/fs module by module.

This is the Rust port's plan.v1. Subsequent revisions (plan.rust.v2 etc.) will capture runtime findings as we go.
