# Plan: sqlite-fs v1

*Stage 2 output — derived from `idea.md`. Review, amend, then proceed to Stage 3 (test engspecs).*

## Module layout

```
src/sqlite_fs/
├── __init__.py              # public re-exports: mkfs, open_fs, Filesystem, errors, types
├── errors.py                # exception hierarchy (13 classes, stdlib co-inheritance)
├── types.py                 # Stat, DirEntry, LockOp, FlockOp, OpenFlags, MountOptions
├── paths.py                 # split, validate, normalize path components
├── schema.py                # DDL, PRAGMAs, migrations, schema_version row
├── nodes.py                 # directory tree ops on the `nodes` table
├── blobs.py                 # chunked content on the `blobs` table
├── xattrs.py                # xattr CRUD on the `xattrs` table
├── symlinks.py              # symlink CRUD on the `symlinks` table
├── locks.py                 # in-memory lock manager (POSIX / OFD / flock)
├── perms.py                 # mode/uid/gid permission checks
├── fdtable.py               # library-level open-fd table (per-inode ref counts, lock ownership)
├── fs.py                    # Filesystem class — orchestrates the above
├── mkfs.py                  # mkfs entry point
├── fsck.py                  # integrity check, orphan GC, consistency repair
└── fuse/
    ├── __init__.py
    ├── adapter.py           # pyfuse3 Operations subclass, bytes↔str, errno mapping
    └── cli.py               # sqlite-fs mount / umount / mkfs / fsck entry points
```

Rationale for the split: **one concern per file**, **one table per file** for the storage-layer modules, **orchestration lives in `fs.py`** (not split across individual modules). `paths.py`, `locks.py`, `perms.py`, `fdtable.py` are pure in-memory logic — they get the strongest engspec trace coverage.

## Public API (refined)

### Module-level entry points

```python
def mkfs(
    path: str,
    *,
    chunk_size: int = 65536,         # 64 KiB; pinned per-filesystem at mkfs time
    overwrite: bool = False,
) -> None

def open_fs(
    path: str,
    *,
    readonly: bool = False,
    uid: int | None = None,           # defaults to os.geteuid()
    gid: int | None = None,           # defaults to os.getegid()
) -> Filesystem
```

### `Filesystem`

Constructor is private — use `open_fs`. Methods grouped:

**Lifecycle**
```python
def close(self) -> None
def __enter__(self) -> Filesystem
def __exit__(self, *exc) -> None
def fsck(self) -> FsckReport
def as_user(self, uid: int, gid: int) -> _UserContext    # context manager
```

The `as_user` context manager temporarily overrides `(uid, gid)` for the duration of the block. This replaces the earlier sketch of per-call uid/gid args — experience with similar libraries shows per-call args clutter every signature. Rationale pinned in an impl-spec Implementation Notes section.

**Directory ops** — `mkdir`, `rmdir`, `readdir` as in idea.md.

**File ops** — use library-level fds (just integers, minted by `open`/`create`, validated on every call):
```python
def create(self, path: str, mode: int = 0o644, flags: int = 0) -> int
def open(self, path: str, flags: int = 0, mode: int = 0o644) -> int
def close_fd(self, fd: int) -> None
def read(self, fd: int, size: int, offset: int) -> bytes
def write(self, fd: int, data: bytes, offset: int) -> int
def truncate_fd(self, fd: int, size: int) -> None
def truncate(self, path: str, size: int) -> None
def fsync(self, fd: int, datasync: bool = False) -> None
def unlink(self, path: str) -> None
```

`flags` matches `os.O_*` (O_RDONLY, O_WRONLY, O_RDWR, O_APPEND, O_CREAT, O_EXCL, O_TRUNC). O_NONBLOCK, O_DIRECT, O_SYNC, O_NOFOLLOW handled (NOFOLLOW honored; DIRECT/SYNC ignored — we're always durable via `synchronous=FULL`).

**Links**
```python
def symlink(self, target: bytes, linkpath: str) -> None
def readlink(self, path: str) -> bytes
def link(self, src: str, dst: str) -> None
```

**Metadata**
```python
def stat(self, path: str, *, follow_symlinks: bool = True) -> Stat
def lstat(self, path: str) -> Stat                              # == stat(..., follow_symlinks=False)
def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None
def chown(self, path: str, uid: int, gid: int, *, follow_symlinks: bool = True) -> None
def utimes(self, path: str, atime_ns: int, mtime_ns: int, *, follow_symlinks: bool = True) -> None
def exists(self, path: str) -> bool
```

Nanosecond timestamps throughout. `follow_symlinks=False` variants support the Linux `AT_SYMLINK_NOFOLLOW` semantics.

**xattrs**
```python
def getxattr(self, path: str, name: str) -> bytes
def setxattr(self, path: str, name: str, value: bytes, *, flags: int = 0) -> None  # XATTR_CREATE | XATTR_REPLACE
def listxattr(self, path: str) -> list[str]
def removexattr(self, path: str, name: str) -> None
```

**Movement**
```python
def rename(self, src: str, dst: str, *, noreplace: bool = False, exchange: bool = False) -> None
```

`noreplace` corresponds to `RENAME_NOREPLACE`, `exchange` to `RENAME_EXCHANGE`. Default (both False) is POSIX rename.

**Locking** — three methods, one per flavor, all advisory:
```python
def posix_lock(self, fd: int, op: LockOp, start: int, length: int, *, wait: bool = False) -> None
def ofd_lock(self, fd: int, op: LockOp, start: int, length: int, *, wait: bool = False) -> None
def flock(self, fd: int, op: FlockOp, *, wait: bool = False) -> None

def posix_getlk(self, fd: int, start: int, length: int) -> LockQuery    # F_GETLK
def ofd_getlk(self, fd: int, start: int, length: int) -> LockQuery
```

`LockOp = Literal["shared", "exclusive", "unlock"]`. `FlockOp` same. `wait=True` is blocking (`F_SETLKW`). `LockQuery` is `(type, pid_or_fd_id, start, length)` or `None` if free. Separate methods keep each flavor's semantics cleanly tied to its API; collapsing into one enum would leak pid/fd differences into callers.

**Mount** — via adapter, not the library:
```python
from sqlite_fs.fuse import mount, umount
def mount(db_path: str, mountpoint: str, *, foreground: bool = False,
          readonly: bool = False, subdir: str | None = None) -> MountHandle
def umount(mountpoint: str) -> None
```

## Internal architecture

### Ownership of the SQLite connection

`Filesystem` owns exactly one `sqlite3.Connection` (write), configured with:
- `PRAGMA journal_mode = WAL;`
- `PRAGMA synchronous = FULL;`
- `PRAGMA foreign_keys = ON;`
- `PRAGMA busy_timeout = 5000;`
- `PRAGMA mmap_size = 0;` (explicit, avoid mmap fidelity issues)

Every mutation runs inside a transaction. Read paths (stat, readdir, read) open a read-only transaction implicitly.

### The `fs.py` orchestrator

Every public method on `Filesystem`:
1. Validates path (`paths.py`)
2. Looks up inodes (`nodes.py`)
3. Checks permissions (`perms.py`)
4. Performs the storage op (`nodes.py` / `blobs.py` / `xattrs.py` / `symlinks.py`)
5. Updates timestamps and `nlink` as required by POSIX
6. Commits

The pattern lets each storage module stay small and single-purpose.

### Lock manager (in-memory)

`locks.py` holds a per-`Filesystem` in-memory registry keyed by inode:
```python
@dataclass
class LockRecord:
    kind: Literal["posix", "ofd", "flock"]
    owner: int                    # pid (posix) or fd_id (ofd, flock)
    start: int                    # byte offset
    length: int                   # 0 means "to EOF/infinity" per POSIX
    type: Literal["shared", "exclusive"]
```

Three flavors coexist per Linux kernel rules (see `man 2 fcntl` "Interaction between record locks and open descriptor locks"). Documented clearly in `locks.engspec`. No disk persistence — crash releases all locks, which is correct POSIX behavior.

### FD table

`fdtable.py` holds open fds:
```python
@dataclass
class FdEntry:
    fd: int
    inode: int
    flags: int
    offset: int                  # for default read/write without explicit offset (not exposed in v1)
    uid: int                     # captured at open time
    gid: int
```

Used by:
- close_fd GC — decrement inode's open_count
- `link` + `unlink` nlink math and orphan GC
- OFD and flock lock ownership

## Test strategy

Five categories. Each category is one test engspec file (or more if large).

### 1. Pure-logic tests (strongest traces)

- `test_paths.engspec` — path parsing. Empty, absolute, trailing slash, `.`/`..`, UTF-8 invalid, too long, embedded NUL, name-too-long.
- `test_locks.engspec` — in-memory lock manager. POSIX merge/split, OFD fd-scoped release, flock whole-file, cross-flavor interactions per POSIX rules.
- `test_perms.engspec` — mode/uid/gid check logic for each op. Root bypass. setuid/setgid files (allowed? In v1, no special semantics — just bits stored).

### 2. Library-level semantics (strong traces)

- `test_nodes.engspec` — mkdir, rmdir, readdir; stat on all kinds; nlink on dirs; UNIQUE(parent, name).
- `test_blobs.engspec` — read/write/truncate across chunk boundaries; zero-pad gaps; shrink-truncate trims chunks; grow-truncate zero-fills; empty file has zero chunks.
- `test_xattrs.engspec` — get/set/list/remove; XATTR_CREATE/REPLACE flags; per-inode isolation; size limits (64 KiB per value).
- `test_symlinks.engspec` — create, readlink, follow-through-stat, lstat, non-UTF-8 targets, symlink chains up to `MAXSYMLINKS` (40).
- `test_fs.engspec` — the orchestrator end-to-end. Every public method with a happy path + 2–3 error paths.
- `test_rename.engspec` — its own file because rename is where every FS has bugs. Same-parent, cross-parent, target-exists-file (overwrite), target-exists-nonempty-dir (ENOTEMPTY), into-own-subtree (EINVAL), atomicity-under-crash.
- `test_hardlinks.engspec` — link + unlink + nlink math + GC on nlink==0 AND open_fds==0; hard-linked file with held fd survives unlink of every path.
- `test_open_flags.engspec` — O_CREAT, O_EXCL, O_TRUNC, O_APPEND, O_NOFOLLOW semantics.

### 3. Durability and crash-safety

- `test_durability.engspec` — write a known sequence, commit, close, reopen, read back identical. Integrity check after every 100 ops. Run at scale (1000+ ops) as a property test.
- `test_crash_safety.engspec` — SIGKILL fixture. Spawn daemon in subprocess, write, SIGKILL mid-write, reopen, assert: (a) `PRAGMA integrity_check == 'ok'`, (b) every committed write is present, (c) any partial write is either fully present or fully absent, never partial.

Traces for these are partially skipped — the SIGKILL fixture is a system-level event. Pure-logic parts (what the schema + WAL guarantee post-recovery) are traced.

### 4. FUSE integration (weaker traces, most marked skipped)

- `test_fuse_basic.engspec` — through a real mount: create, read, write, stat, readdir, rename.
- `test_fuse_symlinks.engspec` — real mount: `ln -s`, `readlink`, symlink chain.
- `test_fuse_permissions.engspec` — real mount: chmod/chown, EACCES.
- `test_fuse_xattrs.engspec` — `getfattr`/`setfattr` CLI tools.
- `test_fuse_locks.engspec` — through a real mount with two subprocess clients exercising flock + fcntl.

Traces marked `<!-- trace: skipped -->` with reason "syscall boundary — kernel-VFS interaction not traceable from spec alone." Library-layer traces cover the underlying semantics; FUSE tests catch translation bugs.

### 5. Interop and benchmarks

- `test_interop.engspec` — `git init; git add; git commit; git log`, `rsync -a`, `rg -r`, `tar -cf`, vim atomic-save dance.
- `test_benchmarks.engspec` — performance bounds from idea.md. Not traced.

## Edge cases worth pinning in engspecs

Each of these becomes an explicit Postcondition or Implementation Note somewhere. They are the "two reasonable implementations would differ" cases.

1. **Empty path `""`** → `PathSyntaxError`.
2. **Non-absolute path (`"foo"`)** → `PathSyntaxError`.
3. **Trailing slash `"/foo/"`** — treat as `/foo` for file/dir ops; reject for `create`-target if target type is file (mirrors POSIX `ENOTDIR`).
4. **`.` and `..` in paths** — reject at `paths.py` level (`PathSyntaxError`). We don't support relative navigation; paths are already-resolved.
5. **Embedded NUL** → `PathSyntaxError`.
6. **Unicode normalization** — we do NOT normalize. `é` (NFC) and `é` (NFD) are different file names. Documented in `paths.engspec` as an Implementation Note.
7. **Case sensitivity** — always case-sensitive. Documented.
8. **Name-byte-length** — enforce ≤ 255 bytes UTF-8 encoded; longer → `NameTooLong`.
9. **Path total length** — enforce ≤ 4096 bytes (POSIX PATH_MAX-equivalent) at `paths.py`. Longer → `PathSyntaxError`.
10. **0-byte file** — valid; `size=0`, zero rows in `blobs`.
11. **Empty directory** — valid; no children; `readdir → []`.
12. **Hard-link to a directory** → `PermissionDenied`/`EPERM` (POSIX forbids).
13. **Symlink to self / infinite loop** — symlinks are opaque targets; loops manifest only on `follow_symlinks=True` operations. Chain depth > 40 → `ELOOP` error (new `class SymlinkLoop(FilesystemError, OSError)`).
14. **Rename edge cases** — pin each in `rename.engspec`: same path no-op, into-own-subtree error, overwrite-file target, non-empty-dir target error, preserves inode + nlink, timestamps updated on both parents.
15. **Lock `length=0`** — per POSIX, "to EOF / infinity". Pin.
16. **Overlapping POSIX locks in same process** — merge if same type, split if different. Pin.
17. **Close fd without unlock** — POSIX releases all that process's locks on any close for that file; OFD releases only this fd's locks; flock releases this fd's flock. Pin each.
18. **Write past EOF** — zero-pads the gap; `stat.size` becomes `offset + len(data)`.
19. **Read past EOF** — returns empty bytes, not error.
20. **chmod/chown/utimes on symlink with `follow_symlinks=True`** — affects target; `False` — affects symlink itself.
21. **Read a directory as file** → `IsADirectory`.
22. **mkdir on existing path** → `AlreadyExists`.
23. **rmdir on non-empty dir** → `DirectoryNotEmpty`.
24. **chunk boundary writes** — a 100 KiB write starting at offset 50 KiB touches chunks 0, 1, 2 (with 64 KiB chunks). Pin the chunking math in `blobs.engspec`.
25. **Integrity after rename** — rename is one SQLite transaction; crash mid-rename leaves either old or new state, never half.
26. **xattr limits** — name max 255 bytes, value max 64 KiB, total per-inode 64 KiB (Linux default). Over limit → `InvalidXattr`.
27. **xattr namespace** — `user.*` unrestricted; `trusted.*` requires root; `security.*` and `system.*` passed through (caller's responsibility, we don't vet).
28. **ctime semantics** — changed on any metadata mutation; NOT settable directly.
29. **atime** — updated on read by default; `MNT_NOATIME`-equivalent option deferred to v2.
30. **Reopen preserves everything** — every test should include a "close + reopen, observe the same" assertion where applicable.

## Ambiguities to resolve

| # | Question | Proposed answer |
|---|----------|-----------------|
| 1 | Chunk size: configurable per mkfs or fixed? | Configurable at `mkfs(chunk_size=...)`, stored in `schema_version` row, immutable thereafter. |
| 2 | `as_user` scope: thread-local or instance-scoped? | Instance-scoped. Making it thread-local adds complexity for v1 single-threaded daemon. |
| 3 | Do we expose `Filesystem` directly or only via `open_fs`? | Only via `open_fs`; `__init__` is private (`_conn` kwarg). |
| 4 | Library fd numbering: monotonic from 1, or random? | Monotonic from 1 per `Filesystem` instance. Fd 0 reserved. |
| 5 | Orphan GC: eager (in `unlink`) or lazy (at close, at fsck)? | **Eager when nlink hits 0 AND open_fds == 0.** Lazy during the held-open phase. `fsck` cleans up any stragglers. |
| 6 | `RENAME_EXCHANGE`: in v1? | YES — it's a single transaction, trivial to implement, useful for atomic swaps. |
| 7 | `RENAME_NOREPLACE`: in v1? | YES — same rationale. |
| 8 | Root FS mode — 0o755? And root owner — 0? | 0o755 + uid 0 + gid 0 at mkfs time; user can `chown` after. |
| 9 | `mkfs(overwrite=True)` semantics? | Truncate and re-create. Require `overwrite=True` explicitly; default is refuse-if-exists. |
| 10 | FUSE foreground vs daemonized default? | Foreground default (easier debug); daemonize is opt-in via `--daemon`. |
| 11 | `subdir=` mount: is the subdir the FS's view, or a chroot? | The subdir is re-rooted — mount exposes only that subtree, `/` at the mountpoint = `subdir` in the DB. |
| 12 | Test fixtures for FUSE mounts: temp mountpoint per-test? | Yes, per-test tmp dir, daemon subprocess, unmount in teardown, `SIGKILL` if stuck. |
| 13 | What does `fsck` actually do? | Verify: PRAGMA integrity_check, no orphan blobs/xattrs/symlinks, no cycles, nlink counts match, no dangling parent refs. Repair orphans; cycle/mismatch → report only. |

Items 5, 10, 11, 13 are the human reviewer's explicit confirmation asks — all others have a defensible default.

## What Stage 3 (test engspecs) will produce

Per test category above, one engspec file per `.engspec` listed in **§ Test strategy**. 13 files total:

```
package/specs/tests/
├── test_paths.py.engspec
├── test_locks.py.engspec
├── test_perms.py.engspec
├── test_nodes.py.engspec
├── test_blobs.py.engspec
├── test_xattrs.py.engspec
├── test_symlinks.py.engspec
├── test_fs.py.engspec
├── test_rename.py.engspec
├── test_hardlinks.py.engspec
├── test_open_flags.py.engspec
├── test_durability.py.engspec
├── test_crash_safety.py.engspec
├── test_fuse_basic.py.engspec
├── test_fuse_symlinks.py.engspec
├── test_fuse_permissions.py.engspec
├── test_fuse_xattrs.py.engspec
├── test_fuse_locks.py.engspec
├── test_interop.py.engspec
├── test_benchmarks.py.engspec
└── conftest.py.engspec
```

21 files. That's the stage-3 deliverable. It's a lot, but every test corresponds to a real contract — the breadth reflects a real FUSE+SQLite filesystem's surface, not methodology bloat.

**Stage-3 convergence order:** `conftest` → `paths` → `locks` → `perms` (foundations) → `nodes` → `blobs` → `xattrs` → `symlinks` (storage modules) → `fs` → `rename` → `hardlinks` → `open_flags` (orchestration) → `durability` → `crash_safety` → `fuse_*` → `interop` → `benchmarks`.

## Ready-to-proceed checklist

- [ ] Reviewer confirms ambiguity #5 (eager GC), #10 (foreground-default mount), #11 (subdir re-root), #13 (fsck scope).
- [ ] Reviewer adds any edge case to `§ Edge cases` that's missing.
- [ ] Reviewer approves the 21-file test engspec plan as the Stage-3 target.

Once those three are green, Stage 3 (write test engspecs) can proceed.
