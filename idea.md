# Idea: sqlite-fs — durable, performant FUSE filesystem backed by SQLite

I want a Linux FUSE filesystem whose entire state — directory tree, file
bytes, xattrs, symlinks, metadata — lives in a single SQLite database.
Mounted, it behaves like a POSIX filesystem for the user and for every
tool that pokes at a directory (`git`, `rg`, `tar`, `rsync`, editors).
Unmounted, it is a `.db` file you can back up, ship, and inspect with
`sqlite3`.

The filesystem must survive power loss without corruption or loss of
committed data. Performance should be competitive with ext4 for common
workloads — not a faithful clone of its latency, but not 10× worse either.

## v1 scope

This is the full v1. FUSE is in scope; advanced POSIX features are in
scope; performance is a first-class concern. FTS and code-symbol indexing
remain deferred (v2/v3).

## Two layers

1. **Core library** (`sqlite_fs`): pure Python class `Filesystem`. All
   filesystem semantics live here. No FUSE dependency. Tests exercise
   it directly and produce engspec traces (strong trace coverage).
2. **FUSE adapter** (`sqlite_fs.fuse`): thin pyfuse3 adapter over the
   library. Translates kernel callbacks to library calls. Minimal logic
   beyond translation. Tests exercise it through real mounts (weaker
   trace coverage by design — syscall boundary).

## Public API (library)

```python
# Lifecycle
def mkfs(path: str) -> None
def open_fs(path: str, *, readonly: bool = False) -> Filesystem

class Filesystem:
    def close(self) -> None
    def __enter__(self) -> Filesystem
    def __exit__(self, *exc) -> None
    def fsck(self) -> FsckReport

    # Directory ops
    def mkdir(self, path: str, mode: int = 0o755) -> None
    def rmdir(self, path: str) -> None
    def readdir(self, path: str) -> list[DirEntry]  # (name, kind, inode)

    # File ops (path-based; for performance, fd-based variants below)
    def create(self, path: str, mode: int = 0o644) -> int  # returns lib-fd
    def open(self, path: str, flags: int, mode: int = 0o644) -> int
    def close_fd(self, fd: int) -> None
    def read(self, fd: int, size: int, offset: int) -> bytes
    def write(self, fd: int, data: bytes, offset: int) -> int
    def truncate(self, fd_or_path: int | str, size: int) -> None
    def fsync(self, fd: int, datasync: bool = False) -> None
    def unlink(self, path: str) -> None

    # Links
    def symlink(self, target: bytes, linkpath: str) -> None
    def readlink(self, path: str) -> bytes
    def link(self, src: str, dst: str) -> None  # hard link, cross-dir OK

    # Metadata
    def stat(self, path: str, *, follow_symlinks: bool = True) -> Stat
    def chmod(self, path: str, mode: int) -> None
    def chown(self, path: str, uid: int, gid: int) -> None
    def utimes(self, path: str, atime_ns: int, mtime_ns: int) -> None
    def exists(self, path: str) -> bool

    # xattrs
    def getxattr(self, path: str, name: str) -> bytes
    def setxattr(self, path: str, name: str, value: bytes,
                 flags: int = 0) -> None
    def listxattr(self, path: str) -> list[str]
    def removexattr(self, path: str, name: str) -> None

    # Movement
    def rename(self, src: str, dst: str) -> None

    # Locking — all three advisory flavors
    def posix_lock(self, fd: int, op: LockOp, start: int, length: int,
                   pid: int) -> None       # fcntl F_SETLK(W), F_GETLK
    def ofd_lock(self, fd: int, op: LockOp, start: int, length: int) -> None
                                           # fcntl F_OFD_SETLK(W)
    def flock(self, fd: int, op: FlockOp) -> None  # BSD LOCK_SH/EX/UN
```

`Stat` fields: `kind`, `size`, `mode`, `uid`, `gid`, `atime_ns`, `mtime_ns`,
`ctime_ns`, `nlink`, `inode`, `blocks`, `blksize`.

`DirEntry` fields: `name: str`, `kind: Literal["file", "dir", "symlink"]`,
`inode: int`.

## FUSE adapter

`sqlite_fs.fuse.mount(db_path, mountpoint, *, foreground=False)` +
a CLI `sqlite-fs mount <db> <mnt>`. Single-user mount — no `allow_other`.
Supported pyfuse3 callbacks:

- `lookup`, `forget`, `getattr`, `setattr`
- `readlink`, `mkdir`, `unlink`, `rmdir`, `symlink`, `rename`, `link`
- `open`, `read`, `write`, `flush`, `release`, `fsync`
- `opendir`, `readdir`, `releasedir`
- `statfs`, `access`, `create`
- `setxattr`, `getxattr`, `listxattr`, `removexattr`
- `getlk`, `setlk`, `setlkw`, `flock`

Not supported (explicit): `ioctl`, `poll`, `bmap`, `fallocate`,
`write_buf`, `read_buf`, `mknod` (special files), `poll`.

## Decisions committed

### Storage

- SQLite single file, **WAL mode**, **`synchronous=FULL`**. Power loss
  must neither corrupt the DB nor lose any transaction that returned
  success to its caller. The performance cost (fsync on every commit)
  is accepted.
- **Chunked blobs** for performance. Content split into fixed 64 KiB
  chunks, keyed `(inode, chunk_id)`. Partial writes touch only affected
  chunks. The last chunk may be short. An empty file has zero chunks.
- **Automatic WAL checkpointing** at default threshold (1000 pages);
  forced checkpoint on `close()`.

### Schema

```sql
nodes (
    inode      INTEGER PRIMARY KEY,
    parent     INTEGER REFERENCES nodes(inode),  -- NULL for root
    name       TEXT,                             -- UTF-8 path component
    kind       TEXT CHECK (kind IN ('file','dir','symlink')),
    mode       INTEGER,
    uid        INTEGER,
    gid        INTEGER,
    size       INTEGER,                          -- files: total content bytes
    atime_ns   INTEGER,
    mtime_ns   INTEGER,
    ctime_ns   INTEGER,
    nlink      INTEGER,
    UNIQUE (parent, name)
);

blobs (
    inode      INTEGER REFERENCES nodes(inode),
    chunk_id   INTEGER,                          -- 0, 1, 2, ...
    data       BLOB,
    PRIMARY KEY (inode, chunk_id)
);

xattrs (
    inode      INTEGER REFERENCES nodes(inode),
    name       TEXT,
    value      BLOB,
    PRIMARY KEY (inode, name)
);

symlinks (
    inode      INTEGER PRIMARY KEY REFERENCES nodes(inode),
    target     BLOB                              -- raw bytes, Linux semantics
);

schema_version (version INTEGER);
```

Indexes: unique `(parent, name)` on `nodes` (already PK in definition).

### Identity & encoding

- Inodes are SQLite `INTEGER PRIMARY KEY` (monotonically increasing,
  never reused within a filesystem). Root inode is 1, created by `mkfs`.
- Path components: UTF-8 `str`. Invalid UTF-8 → `PathSyntaxError`. Byte
  paths are a v2 limitation (documented).
- Name length: ≤ 255 bytes UTF-8-encoded. Longer → `NameTooLong`.
- Symlink targets: raw `BLOB` (bytes). Preserves Linux semantics
  (symlink contents are opaque bytes, possibly non-UTF-8).
- Xattr names: `str` per Linux convention (`user.*`, `security.*`,
  `trusted.*`, `system.*` namespaces). Values: `bytes`.

### Hard links and inode GC

- `link(src, dst)` allowed across directories.
- `nlink` tracked per inode.
- Library tracks open file descriptors per inode.
- When `nlink == 0` AND open-fd-count == 0, the inode, its blobs, its
  xattrs, and its symlink row are garbage-collected in a single
  transaction. Until then, the inode remains addressable via its open
  fds (classic POSIX "unlinked but still held open" semantics).

### Permissions

- **Enforced.** Every operation checks mode + uid + gid against the
  calling context.
- Library API: caller supplies `uid` and `gid` explicitly (via a
  context manager or per-call arg — to be decided in plan stage).
- FUSE adapter: context comes from `pyfuse3.RequestContext`.
- Root (uid 0) bypasses most checks, per POSIX.

### Locking

All three advisory flavors coexist, per Linux kernel semantics:

- **POSIX advisory** (`fcntl F_SETLK / F_SETLKW / F_GETLK`): scoped by
  process (pid). Released on any fd close for that inode within the
  process — the POSIX "broken" semantic.
- **OFD** (`fcntl F_OFD_SETLK / F_OFD_SETLKW`): scoped by open file
  description (fd). Released only when that specific fd is closed. The
  sane alternative to POSIX advisory.
- **BSD `flock`**: scoped by open file description, whole-file only
  (no ranges).

Mandatory locking is **out of scope** (Linux deprecated it).

Locks within a single `Filesystem` instance must be consistent with
themselves; cross-process lock coordination happens through the DB via
a `locks(inode, owner_kind, owner_id, start, length, type)` table,
held in memory for the daemon (disk-backed would be slow and isn't
needed — a crash releases all locks, which is POSIX-compliant).

### Mount

- Single-user. `allow_other` is **not supported** in v1.
- Mount options:
  - `readonly` — refuse all mutations.
  - `subdir=<path>` — expose a subtree rather than the whole root.
- Foreground vs. daemon (fork): both supported.

### Concurrency

- One `Filesystem` instance per `.db` file per process. Constructing
  a second `Filesystem` for the same DB raises `BUSY` (SQLite file lock).
- FUSE daemon: single-threaded event loop in v1. Multi-threaded is
  a v1.1 optimization.

## Performance targets

Treated as soft bounds that inform design, hard-tested via a small
benchmark suite in CI:

- `open`, `stat`, `read 4 KiB`, `write 4 KiB`: ≤ 2× the latency of the
  same op through `pyfuse3.examples.passthroughfs`.
- `rg -r "pattern"` over a Linux-kernel-sized tree: ≤ 3× ext4.
- `git log` in a git repo inside the mount: ≤ 3× ext4.
- 1 GiB sequential read from a single file: ≤ 2× ext4.
- `tar -cf out.tar` over a 10k-file tree: ≤ 3× ext4.

If a target slips by more than 10%, investigate. These are goals,
not blocking invariants.

## Errors

```python
class FilesystemError(Exception): ...
class PathSyntaxError(FilesystemError, ValueError): ...   # bad path, non-UTF-8
class NotFound(FilesystemError, FileNotFoundError): ...
class AlreadyExists(FilesystemError, FileExistsError): ...
class NotADirectory(FilesystemError, NotADirectoryError): ...
class IsADirectory(FilesystemError, IsADirectoryError): ...
class DirectoryNotEmpty(FilesystemError, OSError): ...        # ENOTEMPTY
class PermissionDenied(FilesystemError, PermissionError): ...
class ReadOnlyFilesystem(FilesystemError, OSError): ...       # EROFS
class NameTooLong(FilesystemError, OSError): ...              # ENAMETOOLONG
class InvalidXattr(FilesystemError, OSError): ...             # EINVAL/ENOTSUP
class LockConflict(FilesystemError, BlockingIOError): ...     # EAGAIN/EWOULDBLOCK
class BadFileDescriptor(FilesystemError, OSError): ...        # EBADF
```

Each also has an `errno` attribute so FUSE can map directly.

## Observable invariants

These become tests and are named in impl engspec Invariants sections.

1. No node is its own ancestor (no cycles).
2. `stat(path).size == sum(length(chunk.data) for chunk in blobs where inode=X)`.
3. `readdir` is stable across `close()` + `open_fs()`.
4. `nlink(dir) == 2 + count(subdirs)`.
5. Rename is atomic. After `rename(a, b)`: `a` gone, `b` has `a`'s old
   content and old inode number.
6. Inode numbers are stable across close/reopen.
7. `PRAGMA integrity_check` returns `ok` after any sequence of ops.
8. **Durability**: `SIGKILL` mid-write, remount — the DB is intact and
   contains every transaction that returned success to its caller.
9. Hard link: `stat(A).inode == stat(B).inode`; `unlink(A)` decrements
   `nlink`; GC runs only when `nlink == 0 AND open_fds == 0`.
10. Lock semantics match POSIX advisory + OFD + flock rules.
11. xattrs are per-inode (hard links share xattrs, by design).
12. Read-only mount: every mutation raises `ReadOnlyFilesystem`.

## Scope boundaries — explicitly NOT in v1

- **Full-text search** (v2)
- **Code symbol indexing** (v3)
- **Windows / macOS** — Linux-first
- **`allow_other`** — single-user only
- **Mandatory locking** — advisory only
- **Non-UTF-8 path components** — v2+
- **`ioctl`, `fallocate`, `poll`, `bmap`** — no
- **Multi-writer across processes** on the same DB
- **mmap fidelity under concurrent writes** — documented as "may return
  stale bytes" because pyfuse3's mmap support is limited
- **Network access** of any kind

## Dependencies

Runtime:
- Python 3.10+
- `pyfuse3` (MIT)
- stdlib `sqlite3`

Dev:
- `pytest`
- `hypothesis` (property tests)
- `pytest-xdist` (optional, parallel tests)
- On-host: `libfuse3`, `ripgrep`, `git` (for benchmark tests only)

No network access at runtime. No subprocess dependencies for the
library itself.

## Success criteria for the engspec pipeline on v1

1. `pytest` all green on a fresh checkout with `pip install -e .[dev]`.
2. Every library-layer test's trace is PASS + TRACE_VALID.
3. FUSE-layer tests pass through actual mounts (no stub FUSE).
4. Performance benchmarks meet the soft targets above.
5. A crash-safety harness (`kill -9` the daemon mid-write, remount)
   passes 100 iterations with zero corruption events.

## The v2+ roadmap (context only)

- **v2**: FTS5 search, byte-path support, `allow_other`, multi-threaded
  FUSE daemon.
- **v3**: tree-sitter-based code symbol indexing for `.py`, `.md`,
  `.rs`, `.go`, `.js`, `.ts`.
- **v4**: journaled metadata snapshots for time travel.

---

Please produce a plan at `plan.md` covering: module layout, function
signatures (refining the API sketch above), test strategy by category
(unit / FUSE-mount / concurrency / benchmark / crash-safety), edge cases
worth pinning down before writing engspecs, and any remaining
ambiguities that need human resolution. Reference the worked pipeline
under `~/gits/engspec_code/tests/json_pointer/` for format and rhythm.
