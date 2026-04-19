# Plan v6: in-memory working set with periodic flush

*User question: "how can we keep everything atomic, have the state in memory but also periodically dump to disk in a way that keeps our db state coherent even with power loss?"*

## Concepts separated

- **Atomicity** (all-or-nothing). SQLite gives this in every `synchronous` mode via the WAL / rollback journal. The DB is never half-updated.
- **Durability** (survives power loss). Controlled by `synchronous`. `FULL` = every committed write survives; `NORMAL` = most do, up to ~4 MB of recent writes may be lost; `OFF` = unbounded loss window.
- **Consistency** (readers see a coherent state). Always guaranteed by SQLite regardless of sync mode.

Atomicity does not require fsync; durability does. We can have "in-memory writes, periodic flush" while keeping atomicity by using any sync mode ≥ `OFF` and running `wal_checkpoint` on a timer.

## What SQLite already does

WAL mode gives us the infrastructure:

```
  COMMIT
    → append to WAL (fast; may stay in OS page cache)
    → [sync=FULL:  fsync(WAL) now]
    → [sync=NORMAL: fsync deferred to checkpoint]
    → [sync=OFF:    no fsync from SQLite]

  Every N WAL pages (default 1000) OR on explicit PRAGMA wal_checkpoint:
    → WAL → main DB pages copied
    → fsync(main DB)
    → WAL truncated
```

The missing piece: SQLite's default checkpoint trigger is **page-driven** (`PRAGMA wal_autocheckpoint = 1000`). Low-write-rate workloads can sit in RAM indefinitely.

## plan.v6: time-driven checkpoint

New option `checkpoint_interval_ms` on `open_fs`, `fuse.mount`, and `sqlite-fs mount`. When set, starts a background thread that runs `PRAGMA wal_checkpoint(PASSIVE)` every N ms. The PASSIVE checkpoint:

- Copies any WAL pages that can be copied without blocking writers
- Fsyncs the main DB file
- Frees the WAL pages (truncates on next write)

This **bounds the data-loss-on-power-loss window in time**, not in WAL page count.

## API

```python
open_fs(
    db,
    sync_mode="full" | "normal" | "off",
    checkpoint_interval_ms=None,   # or e.g. 10, 100, 1000
)
```

CLI:

```bash
sqlite-fs mount fs.db /mnt/store --sync-mode normal --checkpoint-interval-ms 10
```

## Implementation

`Filesystem._start_checkpoint_thread(interval_ms)`:
- Opens a second `sqlite3.Connection` to the same DB (SQLite connections aren't thread-safe for sharing by default).
- Sets `PRAGMA busy_timeout = 1000` so checkpoints don't fight the main connection for the writer lock.
- Loops: `wait(interval)` → `execute("PRAGMA wal_checkpoint(PASSIVE)")`.
- Shutdown: `close()` sets a `threading.Event`, joins the thread.

The main connection is untouched — the checkpoint thread operates on its own handle, reading the shared WAL.

## Benchmark (2000 × create+write 4K, 2000 × unlink, 1 × 64 MiB seq write)

```
  sync=full  (default):          create=  116 ops/s   unlink=  237 ops/s   64MB=   82 MiB/s
  sync=normal:                   create= 2107 ops/s   unlink= 4023 ops/s   64MB=  177 MiB/s
  sync=normal + ckpt=100ms:      create= 2039 ops/s   unlink= 3774 ops/s   64MB=  168 MiB/s
  sync=normal + ckpt= 10ms:      create= 2046 ops/s   unlink= 3876 ops/s   64MB=  258 MiB/s
  sync=off    + ckpt= 10ms:      create= 2789 ops/s   unlink= 6144 ops/s   64MB=  760 MiB/s
```

For reference, tmpfs-backed DB (RAM filesystem, no fsync cost at all) was **2316 create/s** and **845 MiB/s** big-write — the `sync=off + ckpt=10ms` mode on SSD is now in the same neighborhood.

## Durability guarantees per configuration

| Configuration | Atomicity | Power-loss data-loss window | OS crash | Process kill (SIGKILL) |
|---|---|---|---|---|
| `sync=full` | ✓ | 0 | 0 | 0 |
| `sync=normal` (no ckpt) | ✓ | up to ~4 MB (1000 WAL pages) | 0 | 0 |
| `sync=normal + ckpt=Nms` | ✓ | up to N ms | 0 | 0 |
| `sync=off + ckpt=Nms` | ✓ | up to N ms of OS-uncommitted | up to N ms of OS-uncommitted | 0 (commits land in OS buffer) |
| `sync=off` (no ckpt) | ✓ | potentially unbounded | unbounded | 0 |

"0" means "every transaction that returned success to its caller survives."

The column **Atomicity is always ✓**: even `sync=off` guarantees that a transaction is either fully present or fully absent after a crash. This is the SQLite WAL + checksumming design.

## Why this is the answer to the user's question

> "Keep everything atomic, have the state in memory but also periodically dump to disk in a way that keeps our db state coherent even with power loss."

- **Atomic**: SQLite transactions, any sync mode.
- **State in memory**: WAL commits stay in OS page cache until checkpoint; SQLite page cache holds hot pages.
- **Periodically dump to disk**: `checkpoint_interval_ms` fires the checkpoint on a timer.
- **Coherent on power loss**: WAL is self-describing with checksums; SQLite recovers the DB to the last valid transaction on next open.

Choose the configuration by how much data you're willing to lose on an abrupt power loss:

- "none at all" → `sync=full` (slow but unloseable)
- "up to N ms" → `sync=normal + ckpt=N` (fast, bounded)
- "I have a UPS or the data is reproducible" → `sync=off + ckpt=N` (fastest, still atomic)

## What stays the same

- Default mode: `sync=full`, `checkpoint_interval_ms=None`. idea.md contract unchanged.
- 183 tests pass — the feature is fully additive.
- No schema change.
