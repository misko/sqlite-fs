# sqlite-fs — Rust port

A Rust port of the [sqlite-fs](https://github.com/misko/sqlite-fs) FUSE filesystem, built from the same engspec artifacts as the Python reference implementation.

## Status

**Spec-first, implementation pending.**

- `package/specs/` — full set of Rust engspecs, ported from `../package/specs/`.
- `src/` — implementation skeleton (module declarations; bodies pending).
- `tests/` — test-engspec bodies pending port.

This directory is an experiment in methodology portability: the idea, plan, and engspecs written for a Python implementation should transfer to Rust with only local (Implementation Notes) rewrites. Design-level documents (`idea.md`, `plan.md`, `plan.v2..v7.md`) are copied verbatim — the constraints they describe don't change when the language does.

## What's different from the Python port

Semantic changes captured in `plan.rust.md`:

- **Errors**: 15 Python exception classes → a single `Error` enum with `#[from] rusqlite::Error` and an `errno(&self) -> i32` method for FUSE translation.
- **Ownership**: `Filesystem` owns its `rusqlite::Connection`, `FdTable`, `LockManager`, watchers, path cache, and checkpoint thread as owned fields — mutated through `&mut self` without `Arc<Mutex>`.
- **Async**: none. `fuser::mount2` is synchronous — the kernel VFS drives the adapter on a dedicated thread.
- **xattr flags**: `os.XATTR_CREATE` / `os.XATTR_REPLACE` → a `bitflags!` wrapper (`XattrFlags::CREATE | XattrFlags::REPLACE`).
- **Paths**: `&str` instead of `str` — non-UTF-8 rejection becomes a compile-time concern at library boundaries.
- **Durability defaults**: identical — `SyncMode::Normal` is the default (plan.v7); `checkpoint_interval: Option<Duration>` (plan.v6).

## Build

```bash
cargo check              # library only
cargo check --features fuse
cargo test
cargo test --features fuse  # requires libfuse3 + fusermount3
```

## Engspecs fully rewritten with Rust semantics

Critical path:
- `src/errors.rs.engspec` — `Error` enum + `errno()`.
- `src/types.rs.engspec` — `Stat`, `NodeKind`, `Access` (bitflags), lock enums.
- `src/paths.rs.engspec` — `parse_path(&str) -> Result<Vec<String>>`.
- `src/schema.rs.engspec` — DDL, `SyncMode`, `apply_pragmas`, `install_schema`.
- `src/mkfs.rs.engspec` — `mkfs` / `open_fs` with `MkfsOptions` / `OpenOptions`.
- `src/lib.rs.engspec` — crate surface.
- `src/nodes.rs.engspec`, `src/entries.rs.engspec` — plan.v3 split CRUD.
- `src/blobs.rs.engspec` — chunked storage with `&[u8]` / `Vec<u8>`.
- `src/symlinks.rs.engspec`, `src/xattrs.rs.engspec` — bytes-first targets, `XattrFlags`.
- `src/fuse/adapter.rs.engspec` — `fuser::Filesystem` impl bridge.

Outstanding (metadata updated; Implementation Notes still show Python):
- `src/fs.rs.engspec` — orchestrator. Rust-shaped outline in `plan.rust.md`.
- `src/fdtable.rs.engspec`, `src/locks.rs.engspec`, `src/perms.rs.engspec`, `src/fsck.rs.engspec`, `src/watch.rs.engspec`, `src/cli.rs.engspec`, `src/fuse/{mod,cli}.rs.engspec`.
- All `tests/*.rs.engspec` — assertion syntax still Python.

## Pipeline

Same engspec-first loop the Python repo uses:

1. Edit `package/specs/src/*.rs.engspec` to reflect any design change.
2. Trace: Claude walks every paragraph of the spec and labels it `PASS` / `FAIL` / `UNCLEAR`. UNCLEAR → spec gap; fix spec first.
3. Regenerate: Claude writes `src/*.rs` from the spec.
4. `cargo test` — the runtime arbiter.

See `../CONVENTIONS.md` (upstream in `engspec_code`) for the enforcement rules.
