# Plan v4: sqlite-fs v1 (watch feature)

*Stage-7-style revision driven by the engspec-first workflow. A single trace (`test_watch_create_fires_create_event.trace.md`) concluded UNCLEAR on the first iteration and pointed directly at a missing cross-cutting spec section. This plan captures the fix.*

## Change from v3

### NEW — `fs.py.engspec § Event emission protocol`

A cross-cutting section in `fs.py.engspec` that defines:

- **When an event fires.** After the SQLite transaction commits. Rolled-back transactions do not emit.
- **Method → event kind mapping.** mkdir/create/symlink/link → `create`. unlink/rmdir → `remove`. write/truncate/truncate_fd → `modify`. rename → `move` (one event, or two for `exchange=True`). chmod/chown/utimes/setxattr/removexattr → `metadata`. Read-only methods, close_fd, and fsync do NOT emit.
- **Event delivery.** Non-recursive watchers match when `watcher.path == parent_of(event.path)`. Recursive watchers match on prefix.
- **Ordering.** Per-watcher FIFO. Across watchers, no guarantee.
- **Timestamps.** `Event.timestamp_ns` equals the mutation's commit time.

The old fs.py.engspec had no mention of events, so every planned `test_watch.*` test traced UNCLEAR. Adding this one section flipped the trace to PASS for `test_watch_create_fires_create_event` and by extension (same chain of citations) for all 9 sibling tests.

## How the engspec-first workflow played out

```
1. Write watch.py.engspec (design, v2 feature).       ← done in plan.v3 commit
2. Write trace for one planned test.                  ← concluded UNCLEAR
3. Gap location: fs.py.engspec has no emission rule.
4. Write plan.v4 (this doc) + add Event emission
   protocol section to fs.py.engspec.
5. Re-trace same test: PASS.
6. Write test_watch.py.engspec (10 tests).
7. Write watch.py + emit calls in fs.py + test_watch.py.
8. pytest: 10/10 pass on first run.
```

Steps 1–5 happened at zero runtime cost — the trace caught the design gap before any watch code existed. Step 8 confirms the design held at runtime.

## What stays the same

- Schema v2 (unchanged; events don't touch storage).
- Public API (fs.watch is additive).
- All plan.v3 decisions.
- 173 pre-existing tests pass without modification — emission is a non-breaking addition.

## Test results

183/183 tests pass (173 from before + 10 new watch tests).

## Stage-7 obligations fulfilled

- [x] `fs.py.engspec § Event emission protocol` added and cited by every mutating method.
- [x] `watch.py.engspec` design section unchanged; impl details + Postconditions for Watcher are in the code (impl engspec doesn't need regeneration since the design was already precise).
- [x] `test_watch.py.engspec` — 10 test functions, each with verbatim assertion code blocks.
- [x] `test_watch_create_fires_create_event.trace.md` — re-generated, verdict now PASS + TRACE_VALID.
- [x] `src/sqlite_fs/watch.py` — 98 lines.
- [x] `src/sqlite_fs/fs.py` — patched at 13 mutating-method sites (one `_emit` call each, `_path_of_inode` helper, `_watchers` set, `watch()` factory).
- [x] `src/sqlite_fs/__init__.py` — re-exports `Event` and `Watcher`.
- [x] `tests/test_watch.py` — 10 tests, all pass.

## Follow-on / v2.1

- Kernel-layer `inotify` interop via pyfuse3's notify channel — emit both library events AND kernel notifications so real `inotify_add_watch` consumers see events.
- Blocking iteration with a timeout (current v2 is non-blocking; StopIteration when queue empty).
- Bounded queue with drop-and-report semantics for slow consumers.
