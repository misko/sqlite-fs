<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_watch.py.engspec -->
<!-- test_function: test_watch_create_fires_create_event -->
<!-- impl_specs: package/specs/src/watch.py.engspec, package/specs/src/fs.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T16:45:00Z -->
<!-- verdict: PASS -->
<!-- regenerated_after: plan.v4 emission protocol -->
<!-- checksum: placeholder-watch-trace-v2 -->

## Subject

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
| `w` | Frame 1 Step 1.2 | `Watcher` attached to `/`, recursive=False |
| `newdir_inode` | Frame 2 Step 2.2 | inode assigned to `/newdir` |
| `event` | Frame 2 Step 2.4 | `Event(kind="create", path="/newdir", node_kind="dir", inode=newdir_inode, ...)` |
| `first` | Frame 3 Step 3.2 | same as `event` — the queued one, dequeued via next() |

## Trace

### Frame 1: as_root.watch("/", recursive=False)

**Cite** — `watch.py.engspec § Filesystem.watch`.

**Step 1.1 — precondition check**
- `/` resolves to a directory ✓
- caller has R access (root, owner-bypass) ✓

**Step 1.2 — construct Watcher**
Per `watch.py.engspec § Watcher.__init__`: registered with `self._fs._watchers.add(self)`.

Bind `w` — new Watcher, path="/", recursive=False, empty queue.

### Frame 2: as_root.mkdir("/newdir")

**Cite** — `fs.py.engspec § fs-6 mkdir (plan.v3 revised)` + `§ Event emission protocol (plan.v4)`.

**Step 2.1 — precondition check** ✓.

**Step 2.2 — commit the mutation**

Per fs-6:
```python
with self._conn:
    new_inode = nodes.insert(self._conn, "dir", mode, uid, gid, now)
    entries.insert(self._conn, parent_inode, name, new_inode)
    nodes.change_nlink(self._conn, parent_inode, +1, now)
    nodes.update_times(self._conn, parent_inode, mtime_ns=now, ctime_ns=now)
```

Bind `newdir_inode = new_inode`.

The `with self._conn:` block commits. The mutation is durable per synchronous=FULL.

**Step 2.3 — emit event**

Cite `fs.py.engspec § Event emission protocol`:
- Method → event kind mapping: `mkdir(path, ...)` → kind="create", path=path.
- Emission happens after the `with` block exits (post-commit).
- Code:
  ```python
  self._emit(Event(
      kind="create", path="/newdir", src_path=None, dst_path=None,
      node_kind="dir", inode=new_inode, timestamp_ns=now,
  ))
  ```

**Step 2.4 — deliver to watcher**

Cite `fs.py.engspec § Event emission protocol § Event delivery`:
- Non-recursive match: `watcher.path == parent_of(event.path)`.
- `event.path = "/newdir"`, `parent_of("/newdir") = "/"`.
- `w.path == "/"` → match ✓.

Cite `watch.py.engspec § Watcher._enqueue`: appends the event to the watcher's internal queue.

Bind `event = Event(kind="create", path="/newdir", src_path=None, dst_path=None, node_kind="dir", inode=newdir_inode, timestamp_ns=now)`.

### Frame 3: next(iter(w))

**Cite** — `watch.py.engspec § Watcher.__iter__`.

**Step 3.1 — queue non-empty check**

Queue has exactly one event (Frame 2 Step 2.4). `next()` returns it.

**Step 3.2 — dequeue**

Bind `first = event`.

### Frame 4: as_root.stat("/newdir").inode

**Cite** — `fs.py.engspec § fs-9 stat`.

Resolves path, fetches the node row for `newdir_inode`, returns `Stat(inode=newdir_inode, ...)`.

## Assertion evaluation

| # | LHS | Resolved | RHS | Op | Result |
|---|-----|----------|-----|-----|--------|
| 1 | `first.kind` | `"create"` | `"create"` | `==` | True |
| 2 | `first.path` | `"/newdir"` | `"/newdir"` | `==` | True |
| 3 | `first.node_kind` | `"dir"` | `"dir"` | `==` | True |
| 4 | `first.inode` | `newdir_inode` | `as_root.stat("/newdir").inode` = `newdir_inode` | `==` | True |

## Verdict: PASS

- All four assertions derive cleanly from `fs-6 mkdir § Postconditions` + `§ Event emission protocol` + `watch.py.engspec § Watcher`.
- The cross-cutting emission protocol is now explicit; no underdetermined steps.
- **The change from UNCLEAR to PASS is the payoff of the engspec-first workflow.** The UNCLEAR verdict in the previous iteration pointed directly at the missing section; adding it flipped every planned watch test trace from "can't derive" to "PASS derivable."

## Verification

<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T16:46:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Staleness: ✓ — emission protocol added this turn, cited in this trace.
- Citation validity: ✓ — `fs-6`, `§ Event emission protocol` (Method → event mapping, Event delivery), `watch.py.engspec § Filesystem.watch` / `§ Watcher.__init__` / `§ Watcher.__iter__` all resolve.
- State consistency: 4/4 references bound before use.
- Verdict consistency: ✓ — 4 True assertion rows → PASS.

### Issues
- none

### Result
- **TRACE_VALID**. The emission protocol is a clean fix: one new section in fs.py.engspec unblocks all 10 planned watch tests. The engspec-first workflow caught the design gap before any code was written.
