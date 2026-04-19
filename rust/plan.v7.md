# Plan v7: default sync_mode changes from `full` to `normal`

*User decision: `sync=normal` becomes the default. Users who need stricter durability opt into `sync=full`.*

## Rationale

Observed (plan.v5 + plan.v6):
- `sync=full`: 116 create/s, 237 unlink/s, 82 MiB/s big-write — limited by ~4 ms fsync per commit.
- `sync=normal`: 2107 create/s, 4023 unlink/s, 177 MiB/s — **18× faster on small-file ops**.

Both modes keep the DB **consistent** on power loss. The difference is the data-loss window:
- `full`: 0 committed transactions lost.
- `normal`: up to ~4 MB of recent writes may be lost (up to the last WAL checkpoint).

Comparison to host filesystems:
- ext4 default (`data=ordered`) can lose recent writes on power loss too; the last few seconds typically.
- btrfs, xfs: similar.
- **`sync=normal` puts sqlite-fs in the same class as mainstream Linux filesystems**: consistent DB, bounded recent-write loss.

The default that made sense at `idea.md` time ("we need to be stable even with power loss, we cannot tolerate corruption") is still met: **NORMAL does not cause corruption**. Only the "zero data loss on power loss" goal softens, and the user is now opting into it consciously based on the 18× speedup.

## What changes

### Defaults
- `mkfs(...)` — unchanged (doesn't open the DB for sustained writes).
- `open_fs(...)` — `sync_mode="normal"` becomes the default.
- `sqlite-fs mount ...` — `--sync-mode normal` is implied if `--sync-mode` is not given.
- `Filesystem.__init__(..., sync_mode="normal")` is the new default.

### Users who want strict durability opt in

```bash
sqlite-fs mount fs.db /mnt/store --sync-mode full
```

or, in Python:

```python
open_fs("fs.db", sync_mode="full")
```

### Checkpoint behavior

`checkpoint_interval_ms=None` (default) means: SQLite's native page-count-based autocheckpoint (every 1000 WAL pages). For callers who want the loss window bounded in **time** rather than in WAL pages, use `--checkpoint-interval-ms 100` or similar (plan.v6).

## Impact on existing test suite

`test_durability.test_synchronous_full` asserts `pragma synchronous == 2` (FULL). With the new default, this test needs to either:
- Become `test_synchronous_normal_is_default` checking `== 1`
- Explicitly pass `sync_mode="full"` and keep the assertion

Going with **both**: rename the existing test to check NORMAL-is-default, and add a new test that explicitly opts into FULL and checks the pragma.

Crash-safety tests (`test_crash_safety.*`) don't specifically check the pragma. They test:
- PRAGMA integrity_check after SIGKILL → still ok under NORMAL.
- Committed writes survive SIGKILL → still holds, because SIGKILL is a process kill, not a power loss; the OS page cache still contains the WAL writes and will flush on next reopen.
- Rename atomicity under crash → still holds (WAL atomicity is the same in all sync modes).
- Remount idempotent → still holds.
- Integrity across repeated crashes → still holds.

So crash-safety tests pass without modification.

## Impact on idea.md

idea.md's scope decision was:

> `synchronous=FULL` for durability. Power loss must neither corrupt the DB nor lose any transaction that returned success to its caller. The performance cost (fsync on every commit) is accepted.

This is revised to:

> `synchronous=NORMAL` by default. Power loss must not corrupt the DB; the last ~4 MB of recent writes may be lost. This matches the durability class of mainstream Linux filesystems (ext4 `data=ordered`, btrfs, xfs) and is the right default for a general-purpose fs. For strict "no data loss" durability, callers explicitly opt into `sync_mode="full"`.

The "no corruption" half of the contract is preserved. The "no committed-transaction loss" half becomes opt-in.

## Migration

No migration needed — existing `.db` files work with the new default. The new default is a runtime choice, not a stored attribute.

## Engspec updates in this plan

- `package/specs/src/schema.py.engspec` — remove the hardcoded `PRAGMA synchronous = FULL` list entry; document the sync_mode parameter.
- `package/specs/src/mkfs.py.engspec` — default parameter `sync_mode="normal"`.
- `package/specs/src/fs.py.engspec` — `Filesystem.__init__` default `sync_mode="normal"`.
- `idea.md` — the durability contract amendment above.

## Result

183 tests pass after the default flip and test_durability update. `sync_mode="full"` remains available; it's just no longer the default.
