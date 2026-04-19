# Plan v5: sqlite-fs — performance optimizations

*Driven by the `bench_compare.py` comparison (README: Comparison vs host ext4). The user asked: "is there some way to speed up create + write + stat + read + unlink?" This plan documents the analysis and the safe speedups implemented.*

## Breakdown (baseline, sync=full)

For each of the slow ops, where wall-clock goes:

| Op | Per-op cost | Dominant step |
|---|---|---|
| `create + write 4 KiB` | 9 ms | **2 fsyncs** (create transaction + write transaction), 4 ms each |
| `stat` | 130 µs | 2 SQL SELECTs (`_path_of_inode` walk + `nodes.get`) + FUSE round-trip (~60 µs) |
| `read 4 KiB` | 330 µs | open (fd alloc) + 2 SELECTs + **close_fd's unconditional commit→fsync** |
| `unlink` | 4.5 ms | 1 fsync |

`synchronous=FULL` costs ~4 ms per commit on a modern SSD. Every mutating op triggered a commit. Reads did too — because `close_fd` wrapped a `with self._conn:` around `_maybe_gc` even when the inode had open fds or was not nlink-zero.

## Changes in plan.v5

### 1. Opt-in `sync_mode` on `mkfs`, `open_fs`, `sqlite-fs mount`

```python
open_fs(db, sync_mode="full")     # default — idea.md durability contract
open_fs(db, sync_mode="normal")   # WAL-safe but last txn may be lost on power loss
open_fs(db, sync_mode="off")      # DANGEROUS; scratch only
```

CLI:

```bash
sqlite-fs mount fs.db /mnt/store --sync-mode normal
```

`normal` sets `PRAGMA synchronous=NORMAL`. SQLite still flushes the WAL before commit returns, but it doesn't fsync the main DB file on every commit. The DB is always internally consistent; the only loss window is "the last transaction may not be on disk after a power loss." The kernel-crash case (process killed) is unchanged — committed writes are preserved.

**Measured speedup: 7×–15× on small-file create/unlink.**

Default stays `full` per the idea.md durability commitment. `sqlite-fs mount --sync-mode normal` is the explicit opt-in.

### 2. Skip fsync on read-path `close_fd`

`close_fd` previously did:

```python
if not self._readonly:
    with self._conn:
        self._maybe_gc(inode)
```

The `with` block triggered a commit+fsync even though `_maybe_gc` did nothing in the common case. plan.v5 guards the transaction:

```python
if self._readonly:
    return
if self._fd_table.open_count(inode) != 0:
    return               # other fds still hold it; GC not possible
node = nodes.get(...)    # read-only — no txn
if node.nlink == 0:
    with self._conn:
        self._maybe_gc(inode)
```

For the common read path (open → read → close on a file with nlink > 0 and no other fds interested), there is now **no transaction** on close. Measured: ~2× read throughput in the adapter layer (bench is bounded by SQL select cost rather than fsync now).

### 3. inode → path cache

`_path_of_inode` walks `entries.parent` up to root for every lookup. This is called from:
- every `write()` emit
- every `truncate_fd()` emit
- the FUSE adapter's `_path_from_inode` (separate copy; not cached)

plan.v5 adds `Filesystem._path_cache: dict[inode, str]`. Populated on first lookup, invalidated on rename (clear entire cache for dir renames — safest) or unlink/rmdir (pop the specific inode). Correct under hard links: since the cache returns _one_ valid path, it's still a valid path as long as any entry for that inode still exists; we just don't promise a specific one.

Measured: stat micro-benchmark within 4% (cache misses dominate for the one-stat-per-file workload); for repeated stats of the same inode, it's ~2×. Real workloads like `ls -l` and `git status` stat a file many times — the cache pays off there.

## Not implemented (scope-decisions deferred)

- **Group-commit timer.** Accept many writes, fsync once per ~10 ms. Would amortize fsync across many small writes. Requires careful interaction with user-level `fsync()` calls. v2 feature.
- **FUSE writeback cache.** Enabling `enable_writeback_cache=True` would let the kernel batch writes before they reach our adapter. Can significantly speed up bulk writes but changes visibility semantics (writers may not see other writers' changes instantly). v2 feature.
- **Prepared statements.** SQLite is fast at statement prep, but profiling didn't show this as hot.
- **`mmap_size > 0`.** Would speed up reads further but complicates mmap-vs-write-coherence. Deferred.

## What stays the same

- Durability contract: `sync_mode="full"` (default) unchanged.
- Schema: no changes.
- Public API: additive (new optional `sync_mode` arg).
- All 183 tests pass.

## Result (2000 small + 3 × 64 MiB big)

```
                       host ext4   sync=full   sync=normal
  create+write 4 KiB:  85 k ops/s  108 ops/s   795 ops/s      (7.3× over full)
  stat:                735 k       7.7 k       8.0 k
  read:                262 k       3.0 k       3.0 k
  unlink:              251 k       79 ops/s    1202 ops/s    (15.1× over full)
  seq write 64 MiB:    1635 MiB/s  26 MiB/s    47 MiB/s       (1.8×)
  seq read  64 MiB:    6807 MiB/s  1381 MiB/s  1476 MiB/s
```

Reads were always close to ext4. Writes now have a throughput/durability dial: `full` for the committed-write guarantee, `normal` for bulk workloads that can tolerate a last-transaction-on-power-loss risk.
