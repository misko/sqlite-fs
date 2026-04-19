# Plan v2: sqlite-fs v1

*Stage 4 output — revised after writing 21 test engspecs in Stage 3. See `plan.md` for the original.*

## Changes from v1

Writing 230+ test functions surfaced thirteen concrete changes. Items marked **NEW** are additions to plan.md; items marked **REFINE** tighten existing plan content; items marked **REMOVE** delete something v1 had.

### 1. NEW — Library test introspection hooks

Writing the tests made clear that several invariants are only observable through SQL, not through the public `Filesystem` API. We introduce five underscore-prefixed hooks used exclusively by tests:

```python
Filesystem._count_chunks(inode: int) -> int
Filesystem._row_exists(table: Literal["nodes","blobs","xattrs","symlinks"], inode: int) -> bool
Filesystem._chunk_size() -> int                      # returns the pinned chunk size
Filesystem._sqlite_pragma(name: str) -> int | str    # reads a PRAGMA value
Filesystem._total_blob_bytes(inode: int) -> int      # sum(length(data)) across chunks
```

**Impact on impl engspec:** `fs.engspec` adds `<file-level: test hooks>` section documenting these as non-public but stable for the test suite. Do not export via `__init__.py`.

### 2. NEW — `SymlinkLoop` error class

Dual-use: raised on symlink chain > MAXSYMLINKS (40), and on `open(..., O_NOFOLLOW)` against a symlink. Both map to POSIX ELOOP. Not in v1's error inventory in plan.md.

```python
class SymlinkLoop(FilesystemError, OSError): ...   # errno = ELOOP
```

**Impact on impl engspec:** `errors.engspec` gets a new `##` section for `SymlinkLoop`; `resolve_path` in `fs.engspec` raises it on the chain-depth check; `open` raises it on the NOFOLLOW-on-symlink check.

### 3. NEW — `InvalidArgument` error class (proposed)

Test engspecs temporarily used `FilesystemError` (the base) and `NotFound` as stand-ins for cases where POSIX returns EINVAL:

- `rename` into own subtree
- `readlink` on a non-symlink
- bad xattr namespace (possibly)

Proposal: add `class InvalidArgument(FilesystemError, ValueError)` with `errno = EINVAL`. Update the three test spots when we write impl specs.

**Open question:** should `SymlinkLoop` sit under `InvalidArgument` or stay its own class? Recommend: own class — they map to different errnos (ELOOP vs EINVAL).

### 4. REFINE — `Access` enum shape

Plan.md said "mode/uid/gid check". The API is pinned by `test_perms.engspec`:

```python
class Access(Flag):
    R = 4
    W = 2
    X = 1

def check_access(node_mode, node_uid, node_gid, caller_uid, caller_gid, access) -> bool
def require_access(...) -> None   # raises PermissionDenied on False
```

Two separate functions: `check_access` is the predicate (returns bool), `require_access` is the raiser. Tests pinned this split.

### 5. REFINE — `FsckReport` shape

```python
@dataclass(frozen=True)
class FsckIssue:
    kind: Literal["orphan_blob", "orphan_xattr", "orphan_symlink",
                  "cycle", "nlink_mismatch", "dangling_parent"]
    inode: int | None
    detail: str

@dataclass(frozen=True)
class FsckReport:
    integrity_check_result: Literal["ok", "corrupted"]
    issues: list[FsckIssue]
```

Plan.md's ambiguity #13 ("what does fsck actually do?") resolved: `fsck` reports all six issue kinds above. It *repairs* `orphan_blob`/`orphan_xattr`/`orphan_symlink` (delete the orphans). It *reports only* `cycle`, `nlink_mismatch`, `dangling_parent` — repairs here can destroy data and must be opt-in (not in v1).

### 6. REFINE — `DirEntry` dataclass is narrow

Plan.md said "readdir returns names, no `.` or `..`" but left the record shape open. Tests pin:

```python
@dataclass(frozen=True)
class DirEntry:
    name: str
    kind: Literal["file", "dir", "symlink"]
    inode: int
```

Nothing else. Mode/uid/gid are a separate `stat()` call. This matches `getdents64` philosophy (readdir is cheap; stat is expensive).

### 7. REFINE — `LockManager` has a public surface, not just `Filesystem` methods

Plan.md buried lock logic inside `locks.py` as an internal helper. `test_locks.py.engspec` tests the `LockManager` class directly, which means it's a published surface for testability:

```python
class LockManager:
    def posix_lock(self, inode, fd_id, pid, op, start, length, *, wait=False) -> None
    def ofd_lock(self, inode, fd_id, op, start, length, *, wait=False) -> None
    def flock(self, inode, fd_id, op, *, wait=False) -> None
    def posix_getlk(self, inode, fd_id, pid, start, length) -> LockQuery | None
    def ofd_getlk(self, inode, fd_id, start, length) -> LockQuery | None
    def on_fd_close(self, inode, fd_id, pid) -> None   # release hook
```

`Filesystem.posix_lock(...)` etc. delegate to the `LockManager` instance. Both surfaces are tested.

### 8. REFINE — `LockQuery` shape

```python
@dataclass(frozen=True)
class LockQuery:
    type: Literal["shared", "exclusive"]
    pid: int                # for POSIX; fd_id encoded as pid for OFD (per man fcntl F_OFD_GETLK)
    start: int
    length: int             # 0 = to EOF
```

### 9. REMOVE — `nosync` mount option

Plan.md ambiguity #5 (crash-safety dial) offered a `nosync` mount option with `synchronous=OFF`. The user's scope decision was "cannot tolerate corruption, stable even with power loss." `synchronous=FULL` is the only setting; `nosync` is removed. Test `test_durability.test_synchronous_full` pins this.

### 10. NEW — Hypothesis stateful property testing

`test_durability.engspec` uses `hypothesis.stateful` for random-op-sequence testing with per-state invariant checks. Adds `hypothesis` as a dev dependency (already in `pyproject.toml`'s `[dev]` extra). Impl specs' Test Strategy sections should note when they are covered by the property test in addition to per-case tests.

### 11. NEW — Extension to engspec format: `trace: skipped`

Stage 3e (FUSE) and 3f (interop, benchmarks) tests cannot be traced cheaply — they depend on kernel-VFS behavior, subprocess coordination, external tools, or runtime measurement. The test engspec format has been extended with per-function metadata:

```markdown
## `test_something(mount)`
<!-- checksum: ... -->
<!-- trace: skipped -->
<!-- trace_skip_reason: syscall boundary | external tool behavior | runtime measurement -->
```

This extension is informal for now. Propose upstreaming it to `engspec_code/engspec_format.md` once we've used it through a full pipeline cycle. The existing `engspec_trace_prompt.md` already documents the skip semantic.

**Count:** 53 of 230+ test functions are `trace: skipped`. The remaining ~180 are fully traceable and comprise the library-layer correctness proof.

### 12. REFINE — Benchmark bounds are generous, not tight

`test_benchmarks.engspec` uses bounds 5–10× looser than the real targets in idea.md. Purpose: avoid CI flakiness. Real performance regressions are caught by a separate (informal) benchmarking pass the developer runs locally. Mark the `benchmark` pytest marker so CI can opt in.

### 13. REFINE — Four plan.md ambiguities resolved

| plan.md # | Question | Resolution (from test-writing) |
|---|---|---|
| 5 | Orphan GC: eager or lazy? | **Eager** when nlink→0 + open_fds==0. fsck is a backstop. `test_hardlinks.test_unlink_last_link_gcs_inode` and `test_open_fd_delays_gc_past_unlink` lock this in. |
| 10 | FUSE foreground vs daemonized default? | **Foreground** default. `test_fuse_basic`'s mount fixture requires `--foreground`. |
| 11 | `subdir=` mount: re-rooted view? | **Re-rooted** — `/` at the mountpoint equals `subdir` in the DB. Not explicitly tested in v1 (single-user mount test suffices); revisit if use case emerges. |
| 13 | What does `fsck` do? | See Change #5 — six issue kinds, three auto-repairable. |

### 14. NEW — Exception errno attribute

Every exception class in `errors.engspec` must expose an `errno` attribute (either class-level or set in `__init__`) so the FUSE adapter can map directly:

```python
class NotFound(FilesystemError, FileNotFoundError):
    errno = errno.ENOENT
```

This is implicit in plan.md (errno-mapping table is in idea.md) but not explicit.

### 15. NEW — `Filesystem.__eq__` on Stat

`test_symlinks.test_stat_follow_false_equals_lstat` compares `Stat` instances with `==`. So `Stat` must be a frozen dataclass with default `__eq__` (which compares all fields). Pin in `types.engspec`.

## What stays the same

- The 15-file module layout from plan.md.
- The schema: 4 tables (`nodes`, `blobs`, `xattrs`, `symlinks`) + `schema_version`.
- The 13-exception inventory (now 14 with `SymlinkLoop`, 15 with `InvalidArgument`).
- All 12 observable invariants.
- All 30 pinned edge cases.
- Test-category organization (5 categories, 21 files).
- Stage-3-convergence order: foundations → storage → orchestration → durability → FUSE → interop.

## Updated ambiguities — still need human resolution

| # | Question | Recommendation |
|---|----------|----------------|
| 16 | Add `InvalidArgument(FilesystemError, ValueError)` class? | **Yes** — cleaner than overloading NotFound for EINVAL conditions. Update test engspecs that temporarily use NotFound. |
| 17 | `SymlinkLoop` covers both chain-too-long AND O_NOFOLLOW refused? | **Yes** — both map to ELOOP in POSIX. Document both triggers in the class's Purpose. |
| 18 | Should `_count_chunks`/`_row_exists`/etc. be enabled in production? | **Yes** — they are cheap and cause no harm. Don't gate behind a test-only flag. |
| 19 | Should we upstream `<!-- trace: skipped -->` to `engspec_format.md`? | **Yes, after Stage 6** — prove it works through one full pipeline cycle first. |

## Stage-5 obligations — every test engspec's requirements on impl engspecs

Each row below is a test engspec file → the impl engspec that must cover its assertions. Any gap at Stage 5 is a spec-to-spec coverage hole.

| Test file | Covers assertions from |
|-----------|------------------------|
| `test_paths` | `paths.engspec § parse_path`, `§ PATH_MAX`, `§ NAME_MAX` |
| `test_locks` | `locks.engspec § LockManager` (7 methods) + `§ LockQuery` |
| `test_perms` | `perms.engspec § check_access`, `§ require_access`, `§ Access` |
| `test_nodes` | `nodes.engspec` (7 ops) + `fs.engspec § stat` + parent-mtime rules |
| `test_blobs` | `blobs.engspec` (read/write/truncate + chunk math) + `fs.engspec` private hook `_count_chunks` |
| `test_xattrs` | `xattrs.engspec` (4 ops) + namespace rules + size limits |
| `test_symlinks` | `symlinks.engspec` (3 ops) + follow chain + MAXSYMLINKS + `fs.engspec § stat(follow_symlinks=...)` |
| `test_fs` | `fs.engspec` (lifecycle, as_user, readonly, fsck) + `mkfs.engspec` + `fsck.engspec` |
| `test_rename` | `fs.engspec § rename` (noreplace + exchange) + atomicity note |
| `test_hardlinks` | `fs.engspec § link`, `§ unlink` + GC invariants + `fdtable.engspec` |
| `test_open_flags` | `fs.engspec § open`, `§ create` + O_* flag handling + `fdtable.engspec` |
| `test_durability` | All storage specs + `schema.engspec` (WAL, synchronous=FULL) + property-test hook |
| `test_crash_safety` | `schema.engspec` durability invariants + crash-subprocess fixture |
| `test_fuse_*` (5 files) | `fuse/adapter.engspec` (errno map, bytes↔str, callback dispatch) |
| `test_interop` | `fuse/adapter.engspec` (observable behavior through the kernel) |
| `test_benchmarks` | Not spec-validated; informational |

## Stage-5 deliverable — impl engspec files

12 impl engspec files under `package/specs/src/`:

```
package/specs/src/
├── __init__.py.engspec         # re-exports
├── errors.py.engspec            # 14 error classes (+ InvalidArgument if approved)
├── types.py.engspec             # Stat, DirEntry, LockOp, FlockOp, Access, LockQuery, FsckIssue, FsckReport
├── paths.py.engspec             # parse_path + constants
├── schema.py.engspec            # DDL, PRAGMAs, migrations
├── nodes.py.engspec             # directory tree CRUD
├── blobs.py.engspec             # chunked content
├── xattrs.py.engspec            # xattr CRUD + size/name limits
├── symlinks.py.engspec          # symlink CRUD
├── locks.py.engspec             # LockManager (3 flavors)
├── perms.py.engspec             # check_access + require_access
├── fdtable.py.engspec           # fd → inode, flags, uid, gid
├── fs.py.engspec                # Filesystem orchestrator
├── fsck.py.engspec              # integrity + repair
├── mkfs.py.engspec              # mkfs + open_fs entry points
├── cli.py.engspec               # sqlite-fs CLI
└── fuse/
    ├── __init__.py.engspec
    ├── adapter.py.engspec       # pyfuse3 Operations subclass
    └── cli.py.engspec           # mount / umount commands
```

18 files counting the FUSE subpackage. One or two might merge during writing (e.g., `mkfs.engspec` could fold into `fs.engspec`).

## Ready-to-proceed checklist

- [ ] Reviewer confirms ambiguities #16 (`InvalidArgument` class), #17 (`SymlinkLoop` dual use), #18 (test hooks always on), #19 (upstream trace-skipped).
- [ ] Reviewer approves the Stage-5 file list (18 impl engspecs).
- [ ] Reviewer approves the coverage table — every test assertion will have a mapped postcondition.

Once those are green, Stage 5 (impl engspecs) can proceed.
