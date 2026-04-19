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

### Comparison vs host ext4 (2000 small files + 3 × 64 MiB big files, plan.v5)

From `scripts/bench_compare.py`:

```
                       host ext4    sync=full    sync=normal
  create+write 4K:     85 k ops/s   108 ops/s    795 ops/s     (7.3× over full)
  stat:                735 k        7.7 k        8.0 k
  read:                262 k        3.0 k        3.0 k
  unlink:              251 k        79 ops/s     1202 ops/s   (15.1× over full)
  seq write 64 MiB:    1635 MiB/s   26 MiB/s     47 MiB/s      (1.8×)
  seq read  64 MiB:    6807 MiB/s   1381 MiB/s   1476 MiB/s

  db size: 194 MiB for 192 MiB content (1% overhead)
```

**Two sync modes (plan.v5).**

- `sync=full` (default) — idea.md's durability contract. Every committed write survives power loss. Cost: ~4 ms fsync per transaction.
- `sync=normal` — opt-in via `open_fs(..., sync_mode="normal")` or `sqlite-fs mount --sync-mode normal`. DB stays consistent across power loss, but the last transaction may be lost. Removes most fsync cost.

```bash
sqlite-fs mount fs.db /mnt/store --sync-mode normal   # 7-15× faster small-file ops
```

**Reads are close to ext4 in both modes** — they don't involve fsync. Sequential read hits 1.4 GB/s through the userspace FUSE daemon serving pages from SQLite's page cache. Plan.v5 also added a hot-path optimization: `close_fd` no longer opens a transaction when GC is not eligible (skips fsync on read-close). And an inode→path cache shaves ~4% off cold stat, ~2× on warm.

**Writes pay the durability cost.** Every mutation is its own SQLite transaction. `sync=full` does a real fsync per commit — ext4 coalesces thousands of writes into a handful of disk commits; sqlite-fs commits each one. `sync=normal` keeps the WAL safe but doesn't fsync the main DB, recovering most of the small-file throughput gap.

**Storage overhead ~1%**: 192 MiB of content → 194 MiB DB file.

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

sqlite-fs was produced in one conversation, driven by the engspec-first methodology. Each prompt below is the actual user message (verbatim or lightly condensed), in order. The summary on the right is what the agent did in response.

**~30 prompts, most of them `"keep going"` or a few words.** The methodology does the lifting — the engspec package, traces, and regenerated code handle the bookkeeping.

| # | User prompt | Agent response (1 line) |
|---|---|---|
| 1 | "Please review `~/gits/engspec_code` and prepare an elegant English-first approach to implement sqlite-fs." | Read the engspec prompts; drafted `PLAN.md` with 3-layer SQLite-backed FUSE design. |
| 2 | "Carefully consider which test cases we want. Make engspecs for the tests first. Update the plan." | Rewrote plan tests-first: 10 test-spec files with verbatim assertion-block examples. |
| 3 | "What's a good way to implement engspec? Start with tests for engspec then iterate?" | Explained that engspec's 3/3 regen IS its test; recommended a vertical slice first. |
| 4 | "Can engspec have tests? Claude should be able to trace through engspec and evaluate PASS/FAIL." | Designed the `.trace.md` concept — verdicts derived from spec alone, no code. |
| 5 | "Add an engspec command/skill for PASS/FAIL/UNCLEAR. Design the trace format." | Drafted `TRACE_SKILL_PLAN.md`: format + generator + separate verifier for proof-checking. |
| 6 | "Update `engspec_code`, add test cases. What's a good simple app?" | Proposed RFC 6901 JSON Pointer (real spec, ground-truth vectors); listed files to add. |
| 7 | "This looks great, let's do it!" | Wrote `engspec_trace_format.md` + generator + verifier + JSON Pointer 8-stage harness. |
| 8 | "Propose a commit message and files for engspec_code." | Proposed 3-file commit + message. |
| 9 | "Please push this for engspec_code." | Pushed `05aa6d1` to `misko/engspec_code`. |
| 10 | "Add a section to engspec_code README: idea → plan → engspec tests → revise → run traces → iterate." | Wrote the 'Building from scratch' workflow section. |
| 11 | "Yes this looks great." | Pushed README update. |
| 12 | **"Let's start sqlite-fs from scratch using this pattern."** | Archived old plans; proposed library-first v1. |
| 13 | "It needs FUSE, locking, advanced FS features, and be performant." | Listed scope additions + 6 questions (locks, hard links, perf, mounts, durability, symlinks). |
| 14 | "1) all three, 2) yes, 3) great, 4) single user, 5) stable even with power loss, 6) recommend." | Recommended BLOB symlink targets; rewrote `idea.md` for full v1 scope. |
| 15 | "This looks great." | Produced `plan.md`: 15 modules, 21 test files, 30 edge cases, 13 ambiguities. |
| 16 | "Can you please add package install support." | `pyproject.toml` with `[fuse]`/`[dev]`/`[all]` extras + CLI entry point. |
| 17 | "Great, let's keep going!" | Foundations test engspecs: conftest + paths (15) + locks (17) + perms (12). |
| 18 | "Keep going." | Storage + orchestration engspecs (~100 more tests). |
| 19 | "Keep going." | Finished Stage 3: durability, crash_safety, FUSE × 5, interop, benchmarks. 21 files, ~230 tests. |
| 20 | "Yes." | `plan.v2.md` (15 test-surfaced revisions) + 19 impl engspec files. |
| 21 | "Sounds good, keep going." | 5 traces (all PASS); regenerated pure-logic code. **44/44 tests pass.** |
| 22 | "Yes please." | Committed in 3 logical slices; continued. |
| 23 | "Yes." | Regenerated SQLite-backed code; caught `create()` O_WRONLY finding. **149/149 tests pass.** |
| 24 | "Please keep pushing, is everything working in Python/Linux?" | Installed `pyfuse3`; wrote FUSE adapter; fixed POSIX `openat(O_CREAT, 0o444)` semantics from `git add`; `git`/`rsync`/`tar`/`venv`/`sqlite-in-mount` all work. |
| 25 | "Please keep pushing." | `test_crash_safety.py` (SIGKILL fixture), bench script, hypothesis stateful property test. **173 tests pass.** |
| 26 | "Do we have watch-dir support? Is engspec first-class — do we start with engspec before any code?" | No to watch yet; engspec-first is the intent but engspec debt had accumulated. |
| 27 | "Run the engspec implementation pipeline for this. Is the approach documented in engspec_code? An engspec-first repo should always start with engspec before any code." | Paid down engspec debt; wrote `watch.py.engspec` design-only; drafted `CONVENTIONS.md` for engspec_code. |
| 28 | "Update engspec_code and push, then keep going in this repo." | Pushed `CONVENTIONS.md` upstream; ran the full engspec-first loop on watch: added emission protocol → UNCLEAR→PASS retrace → tests → impl → **10/10 watch tests pass, 183/183 total.** |
| 29 | "Push to `git@github.com:misko/sqlite-fs.git`. Summarize all interactions in the README." | Pushed `main`; drafted the methodology summary. |
| 30 | "How fast is sqlite-fs vs our current partition? Add a benchmark simulating lots of small and a few big files." | Wrote `scripts/bench_compare.py`. See **Comparison vs host ext4** section below. |
| 31 | "Focus on the explicit prompts I gave, 1-line responses. Show how easy this was." | This table. |

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
