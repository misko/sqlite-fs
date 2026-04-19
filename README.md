# sqlite-fs

A Linux FUSE filesystem whose entire state — directory tree, file contents, xattrs, symlinks, metadata — lives in a single SQLite database. Mounted, it behaves like POSIX. Unmounted, it is a portable `.db` file you can back up, ship, and inspect with `sqlite3`.

**Status: working v1.** 183 tests passing. Real tools work on a live mount: `git`, `rsync`, `tar`, `grep`, `python -m venv`, `sqlite3` inside the mount. Built with the [engspec methodology](https://github.com/misko/engspec_code) — English-first specification, traces before code, regeneration from specs, engspec-first (spec changes precede code changes).

## What works right now

- POSIX ops: mkdir, create, read, write, stat, readdir, rename (incl. `RENAME_EXCHANGE`), symlink, hard link, unlink, truncate, chmod, chown, utimes
- xattrs via `os.setxattr` / `os.getxattr` / `os.listxattr`
- Full three-flavor advisory locking at the library layer (POSIX, OFD, BSD flock)
- Power-loss durability: `synchronous=FULL`, WAL mode. Crash-safety tests SIGKILL the daemon mid-write; every committed transaction survives.
- In-process directory watching with event subscriptions (`fs.watch(path, recursive=...)`), recursive or non-recursive; `create`/`remove`/`modify`/`move`/`metadata` events.
- `sqlite-fs mkfs | mount | umount | fsck | export` CLI.
- `git init; git add; git commit` works end-to-end inside the mount.
- `python -m venv` creates a full virtualenv inside the mount.
- A SQLite DB stored inside sqlite-fs round-trips correctly (dogfooding).

## Benchmark snapshot (single-threaded, synchronous=FULL)

```
library-direct:                    through-FUSE:
  stat hot:        22 µs/op          stat hot:       141 µs/op
  read 4 KiB hot:   7 µs/op          read 4 KiB:       ~
  mkdir:         3.5 ms/op           mkdir:            5 ms/op
  create+write:  7.1 ms/op           create+write:     9 ms/op
  seq write:     16 MiB/s            seq write:       15 MiB/s
  seq read:       1 GiB/s            seq read:       400 MiB/s
```

Write-path cost is dominated by SQLite `synchronous=FULL` fsync. Durability was chosen over throughput per `idea.md`.

## Install

```bash
pip install -e .              # library only
pip install -e ".[fuse]"      # + pyfuse3 (needs libfuse3 system lib)
pip install -e ".[dev]"       # + pytest, hypothesis
pip install -e ".[all]"       # everything
```

## Usage

```bash
sqlite-fs mkfs /tmp/store.db
sqlite-fs mount /tmp/store.db /mnt/store --foreground

# ... do normal POSIX things under /mnt/store ...

sqlite-fs umount /mnt/store
sqlite-fs fsck /tmp/store.db
```

## Design

- **Storage:** SQLite single file, WAL mode, `synchronous=FULL`. Power loss must neither corrupt the DB nor lose committed writes.
- **Layers:** pure-Python `sqlite_fs.Filesystem` library underneath a thin `pyfuse3` adapter.
- **Schema (plan.v3):** `nodes` (inode metadata) + `entries` (directory entries: `parent`, `name`, `inode`) separated — so multiple entries can point at one inode for hard links. Plus `blobs` (chunked content), `xattrs`, `symlinks`.
- **Content:** chunked blobs (64 KiB default).
- **Locking:** all three advisory flavors coexist — POSIX (`fcntl F_SETLK`), OFD (`F_OFD_SETLK`), BSD (`flock`).
- **Hard links** across directories, full `nlink` tracking, inode GC when `nlink == 0` AND no open fds reference it.
- **v1 does NOT include:** full-text search (v2), code symbol indexing (v3), `allow_other` mounts, mandatory locks, non-UTF-8 path components, Windows/macOS.

---

## How this repo was built

This README is a record of the engspec-first methodology applied in practice. The conversation that produced sqlite-fs followed ~40 turns, each a discrete step in the pipeline documented at [`misko/engspec_code`](https://github.com/misko/engspec_code). The summary below captures the key interactions — not for nostalgia, but because the methodology's value is best seen by tracing where each engspec revision came from.

### Phase 0 — methodology work in `engspec_code`

Before any sqlite-fs code, the methodology itself was extended:

1. Designed a **trace mechanism** (`.trace.md` format) that derives **PASS / FAIL / UNCLEAR** for a test engspec from spec alone, without running code. UNCLEAR names the exact missing spec section.
2. Added `engspec_trace_format.md`, `engspec_trace_prompt.md`, `engspec_verify_trace_prompt.md` to `engspec_code`.
3. Built a worked example: JSON Pointer (RFC 6901). 8-stage pipeline (`idea → plan → test engspec → plan.v2 → impl engspec → trace → regenerate → pytest`) validated with 25/25 tests passing on first regeneration.
4. Published `CONVENTIONS.md` in `engspec_code` distinguishing **engspec-first** repos (this one) from **engspec-retrofitted** repos.

### Phase 1 — sqlite-fs v1 scope

5. **Proposed library-first v1** (no FUSE initially) because traces are strongest over pure logic.
6. **User pushed back**: v1 must include FUSE + all three advisory lock flavors + hard links with GC + `synchronous=FULL` durability + performance targets. Six scope questions answered:
   - All three locking flavors.
   - Hard links across directories, with `nlink` GC.
   - ≤ 2× pyfuse3 overhead as soft perf bound.
   - Single-user mount (no `allow_other`).
   - Cannot tolerate corruption — `synchronous=FULL`.
   - BLOB symlink targets (Linux raw-byte semantics).
7. Rewrote `idea.md` with the expanded scope. Saved to memory so future sessions know the v1 commitments.

### Phase 2 — plan + test engspecs

8. Produced `plan.md` (15 source modules, 5 test categories, 30 pinned edge cases, 13 ambiguities flagged for reviewer confirmation).
9. Added package-install scaffolding: `pyproject.toml`, `src/sqlite_fs/` skeleton, stub CLI entry point.
10. Wrote **21 test engspec files** (~230 test functions, ~5,700 lines) in five substages:
    - **foundations**: `conftest`, `test_paths`, `test_locks`, `test_perms`
    - **storage**: `test_nodes`, `test_blobs`, `test_xattrs`, `test_symlinks`
    - **orchestration**: `test_fs`, `test_rename`, `test_hardlinks`, `test_open_flags`
    - **durability + crash**: `test_durability`, `test_crash_safety`
    - **FUSE + interop**: `test_fuse_basic`, `_symlinks`, `_permissions`, `_xattrs`, `_locks`, `test_interop`, `test_benchmarks`
11. Wrote `plan.v2.md` capturing 15 changes surfaced by test-writing (private introspection hooks, `SymlinkLoop` + `InvalidArgument` errors, `Access` enum, `FsckReport` shape, `nosync` option dropped, `<!-- trace: skipped -->` format extension).

### Phase 3 — impl engspecs + traces

12. Wrote **19 impl engspec files** (~3,900 lines) mirroring the source modules: `errors`, `types`, `paths`, `schema`, `perms`, `fdtable`, `locks`, `nodes`, `blobs`, `xattrs`, `symlinks`, `fs`, `mkfs`, `fsck`, `cli`, plus `fuse/` subpackage.
13. Wrote **5 representative traces** before any code. All concluded PASS + TRACE_VALID on first derivation. One minor quality note: `ofd_lock` Postconditions cross-references `posix_lock` rather than inlining the `kind="ofd"` scoping.

### Phase 4 — regenerate code, run tests

14. Regenerated **pure-logic modules** (errors/types/paths/perms/locks) + three test files. **44/44 tests passed on first regeneration.**
15. Regenerated **SQLite-backed modules** (schema/fdtable/nodes/blobs/xattrs/symlinks/fs/mkfs/fsck) + storage + orchestration tests. **149/149 tests passed** after one runtime finding (`create()` returning `O_WRONLY` blocked `fd.read()` — fixed by delegating to `O_RDWR`; recorded in `plan.v3.md`).

### Phase 5 — hard links via the engspec-first loop (plan.v3)

16. Hit a schema tension: `fs.link()` couldn't work against the v1 schema (two directory entries sharing one inode PK is impossible). Wrote `plan.v3.md` proposing the `nodes` / `entries` split.
17. **Wrote a trace for `test_link_creates_hard_link` against the revised spec chain before touching code.** Verdict: PASS at spec level.
18. Regenerated `schema.py`, `nodes.py`, new `entries.py`, and patched `fs.py` throughout. Wrote `test_hardlinks.py` from its engspec. **13 new tests passed on first run.** Combined: 162/162.

### Phase 6 — FUSE adapter + real-world interop

19. Installed `pyfuse3`, wrote `src/sqlite_fs/fuse/adapter.py` (~290 lines, 27 kernel-VFS callbacks). Mount worked through the kernel on first attempt.
20. Runtime finding: **`git add` returned EACCES** on a freshly-created `0o444` object file. `openat(O_CREAT|O_RDWR|O_EXCL, 0o444)` is POSIX-legal for the creator — the mode-based access check must be skipped for newly-created files. Fix recorded as plan.v3 finding #3.
21. Runtime finding: `mkfs` hardcoded root ownership to `uid=0`, making mkdir at root fail for non-root mounters. Defaulted `mkfs(owner_uid=None)` to `os.geteuid()`.
22. Full interop smoke test: `git init/add/commit/log`, `rsync -a` with mtime preservation, `tar -cf/-xf` round-trip, `grep -r`, `python -m venv`, **a SQLite DB inside sqlite-fs** (exercises fsync, lock, seek, pwrite on a real workload). All green.

### Phase 7 — durability + benchmarks

23. Wrote `test_crash_safety.py` — 5 tests using a SIGKILL-subprocess fixture. Verifies integrity_check, committed-write survival, rename atomicity under crash, idempotent remount, integrity across multiple crash cycles. All pass.
24. Wrote `scripts/bench.py` — profiled both library-direct and through-FUSE (see Benchmark snapshot above).
25. Wrote `test_durability.py` including a **hypothesis stateful property test**: 25 examples × 20 random ops each = 500 op sequences, asserting integrity-check + size-matches-blob + readdir-matches-expected invariants after every step. All green. (Fixed one shadowing bug: `_chunk_size` was both attribute and promised test-hook method.) **173/173.**

### Phase 8 — engspec-first watch feature (plan.v4)

26. User question: "is this engspec-first documented, and do we start with engspec before any code?" Answered: yes in principle, but engspec debt had accumulated (runtime findings in code but not in impl specs). Promised to pay it down.
27. **Updated `fs.py.engspec`, `mkfs.py.engspec`, `fuse/adapter.py.engspec`** with the runtime findings from plan.v3. Wrote `watch.py.engspec` as a **design-only v2 engspec** — no code yet.
28. Wrote a trace for a planned watch test — **verdict UNCLEAR.** Trace pointed at the missing `fs.py.engspec § Event emission protocol` section. No code existed yet; cost of catching the gap: one trace.
29. Pushed `CONVENTIONS.md` to `misko/engspec_code` formalizing the engspec-first commitment.
30. Added `fs.py.engspec § Event emission protocol` (cross-cutting spec naming which mutating methods emit which events, when, and how watchers match). Re-traced: **PASS + TRACE_VALID.**
31. Wrote `test_watch.py.engspec` (10 test functions). Wrote `src/sqlite_fs/watch.py` (`Event`, `Watcher`). Patched `fs.py` at 13 mutating-method sites. **All 10 watch tests passed on first run. Total: 183/183.**

### Methodology findings captured along the way

The workflow surfaced six spec-level findings, each recorded in a `plan.vN.md`:

| # | Finding | Surfaced where |
|---|---------|----------------|
| 1 | `create()` should return O_RDWR, not O_WRONLY | pytest runtime |
| 2 | `nodes` and `entries` must be separate tables for hard links | implementation time |
| 3 | POSIX: creator of a file is allowed to open with any flags regardless of mode bits | `git add` at runtime |
| 4 | `mkfs` must default root owner to caller euid/egid, not uid=0 | FUSE mount EACCES |
| 5 | `_chunk_size` attribute shadowed the same-named test hook method | hypothesis stateful test |
| 6 | Mutating methods need a cross-cutting emit protocol for watch to work | UNCLEAR trace before any code |

**Finding #6 is the one the methodology is most proud of.** Every other finding surfaced through code (at pytest, at implementation, or at real-tool runtime). #6 surfaced at trace time, with zero code written. The cost of fixing it was one spec-section addition; the cost of *not* catching it would have been rewriting every mutating method after the first watch test failed.

### Pipeline artifacts in this repo

```
idea.md                          # Phase 1: v1 scope
plan.md, plan.v2.md              # Phase 2: module design + test-surfaced revisions
plan.v3.md                       # Phase 5: schema split + runtime findings
plan.v4.md                       # Phase 8: event emission protocol
package/specs/tests/*.engspec    # Phase 2: 22 test engspec files, ~230 tests
package/specs/src/*.engspec      # Phase 3: 20 impl engspec files
package/traces/*.trace.md        # Phase 3, 5, 8: traces driving the impl specs
src/sqlite_fs/*.py               # Phase 4-8: regenerated Python
tests/*.py                       # Phase 4-8: regenerated tests (183/183 pass)
scripts/bench.py                 # Phase 7: performance profile
archive/                         # Pre-methodology planning (for history)
```

## Development

Hack by running `pytest` and following the engspec-first rule: **no code change without a spec change first.** If you find a runtime bug, the fix sequence is:

1. Write the finding into a new `plan.vN.md`.
2. Update the affected engspec.
3. Re-trace the affected test from the revised spec; confirm PASS.
4. Regenerate or patch the code.
5. Re-run `pytest`.

See [`misko/engspec_code`](https://github.com/misko/engspec_code) for the methodology prompts and `CONVENTIONS.md` for engspec-first repo conventions.
