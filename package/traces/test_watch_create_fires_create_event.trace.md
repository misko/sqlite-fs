<!-- engspec-trace v1 -->
<!-- test_spec: planned in v2 — not yet written -->
<!-- test_function: test_watch_create_fires_create_event -->
<!-- impl_specs: package/specs/src/watch.py.engspec, package/specs/src/fs.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T16:30:00Z -->
<!-- verdict: UNCLEAR -->
<!-- design_trace: true -->
<!-- checksum: placeholder-watch-trace -->

## Subject (hypothesized test body)

```python
def test_watch_create_fires_create_event(as_root):
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.mkdir("/newdir")
        first = next(events)
        assert first.kind == "create"
        assert first.path == "/newdir"
        assert first.node_kind == "dir"
        assert first.inode == as_root.stat("/newdir").inode
```

## Given
- `as_root` fixture: mkfs'd, opened, uid=0, gid=0.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `w` | Frame 1 | `Watcher` attached to `/`, non-recursive |
| `event` | Frame 3 | emitted Event with kind="create", path="/newdir", ... |
| `newdir_inode` | Frame 2 | inode assigned by `nodes.insert` to `/newdir` |

## Trace

### Frame 1: as_root.watch("/", recursive=False)

**Call** — `Filesystem.watch(path="/", recursive=False)`
**Cite** — `watch.py.engspec § Filesystem.watch`

**Step 1.1 — precondition check**
- `path` resolves to a directory (root is a dir) ✓
- caller has R access (root, owner) ✓

**Step 1.2 — construct Watcher**
Cite `watch.py.engspec § Watcher.__init__`: returns a `Watcher` registered with this `Filesystem`. Events from mutations after this point are queued.

Bind `w` = new `Watcher`.

### Frame 2: as_root.mkdir("/newdir")

**Call** — `Filesystem.mkdir(path="/newdir", mode=0o755)`
**Cite** — `fs.py.engspec § fs-6 mkdir (plan.v3 revised)`

**Step 2.1 — precondition check** ✓ (root writable by caller).

**Step 2.2 — insert + entry**
Cite fs.py.engspec § fs-6:
```python
new_inode = nodes.insert(self._conn, "dir", mode, uid, gid, now)
entries.insert(self._conn, parent_inode, name, new_inode)
nodes.change_nlink(self._conn, parent_inode, +1, now)
```

Bind `newdir_inode = new_inode`.

**Step 2.3 — emit event** — ? UNDETERMINED

Cite `watch.py.engspec § Implementation approach (library-only)`:
> "Every mutating method (mkdir, rmdir, ...) calls `self._emit(Event(...))` after commit."

Is this visible in fs.py.engspec § fs-6 mkdir? **Answer: no, not yet.** The watch.engspec describes what `fs.mkdir` SHOULD do, but the fs.py.engspec § fs-6 has not been revised to cite `self._emit`. Two specs disagree on whether `mkdir` emits events:
- `watch.engspec` says: yes, after commit.
- `fs.engspec § fs-6` says: nothing about events.

This is the classic spec-gap that tracing catches: one spec promises a behavior, another spec governs the implementation and doesn't mention it.

### Frame 3: next(iter(w))

**Call** — pull next event from the watcher's queue.
**Cite** — `watch.py.engspec § Watcher`

**Step 3.1 — queue-empty check**

Can we derive that `w`'s queue has exactly one event right now?
- If Frame 2's `_emit` fired: yes, queue has the create event.
- If Frame 2's `_emit` did NOT fire (because fs.engspec § fs-6 doesn't mention it): queue is empty, `next()` would block forever (or raise StopIteration — `watch.engspec § Watcher.__iter__` does not specify which).

**This step cannot be derived from the current specs as they stand.**

## Verdict: UNCLEAR

**Underdetermined steps**
- Frame 2 Step 2.3: `watch.engspec` says `fs.mkdir` emits; `fs.engspec § fs-6` does not mention events. Two specs in tension.
- Frame 3 Step 3.1: queue state depends on Frame 2's tension; cascades.
- Watcher queue semantics on empty: blocking or StopIteration? `watch.engspec` does not specify.

**Gap location** — `fs.py.engspec § fs-6 mkdir § Postconditions` must be amended to add:
> "After the SQLite transaction commits, emit an Event(kind='create', path, node_kind='dir', inode=new_inode, timestamp_ns=now) to every registered Watcher whose path matches. 'Matches' = (watcher.path == parent_path of the created node) OR (watcher.recursive AND watcher.path is a prefix of the created path)."

And analogous additions to fs-7 (rmdir), fs-8 (readdir — NO emit, read-only), fs-10 (open/create/write/truncate_fd/unlink), fs-11 (symlink/link), fs-12 (chmod/chown/utimes), fs-13 (xattrs), fs-14 (rename).

Plus `watch.engspec § Watcher` must specify iteration semantics on empty queue — blocking-with-timeout? StopIteration only after close()? Design call.

**Why it matters**
Every test in the planned `test_watch.py.engspec` depends on this coupling between mutating methods and the watcher queue. All ten planned watch tests will trace UNCLEAR until these cross-cutting emit additions are made.

**Suggested spec strengthening**
Add a new top-level section `## Event emission protocol` to `fs.py.engspec` describing which library methods emit which events, and their exact timing (after commit). Then amend each method's Postconditions with a reference.

Alternatively: factor emission into a decorator or helper in watch.py, so individual method specs can say "emits per `watch.py.engspec § emission rules`."

## Methodology note

This trace is the **whole point** of the engspec-first approach: the watch feature has no code yet, but we can already see that the cross-cutting concern (mutation emits events) needs to be spec'd BEFORE any code is written, otherwise there will be no consistent way for `fs.mkdir` / `fs.unlink` / etc. to know they should emit. The trace surfaces this now, at zero cost. If we had written watch.py first, we'd have discovered it when wiring the first test.

Planned resolution: revise `fs.py.engspec` with an emission protocol section; re-trace this test; expect PASS on the next iteration.
