<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_hardlinks.py.engspec -->
<!-- test_function: test_link_creates_hard_link -->
<!-- impl_specs: package/specs/src/fs.py.engspec, package/specs/src/nodes.py.engspec, package/specs/src/entries.py.engspec, package/specs/src/blobs.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T15:10:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-link -->

## Subject

```python
fd = as_root.create("/a")
as_root.write(fd, b"content", offset=0)
as_root.close_fd(fd)

as_root.link("/a", "/b")

# Both paths point to the same inode.
assert as_root.stat("/a").inode == as_root.stat("/b").inode

# nlink bumped to 2.
assert as_root.stat("/a").nlink == 2
assert as_root.stat("/b").nlink == 2

# Content visible through both paths.
fd = as_root.open("/b", flags=0)
assert as_root.read(fd, size=7, offset=0) == b"content"
as_root.close_fd(fd)
```

## Given
- `as_root` fixture: fresh mkfs'd fs, acting as uid=0, gid=0.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `inode_a` | Frame 1 Step 1.4 | new inode minted by `nodes.insert(kind="file", ...)` — call it N. |
| `entry_a` | Frame 1 Step 1.5 | entries row `(parent=1, name="a", inode=N)` |
| `inode_b_returned` | Frame 4 Step 4.5 | same `inode_a = N` — hard-link semantic |
| `entry_b` | Frame 4 Step 4.4 | entries row `(parent=1, name="b", inode=N)` |
| `nlink_after_link` | Frame 4 Step 4.6 | 2 — incremented from 1 via `nodes.change_nlink(N, +1)` |

## Trace

### Frame 1: as_root.create("/a")

**Call** — `Filesystem.create(path="/a", mode=0o644, flags=0)`
**Cite** — `fs.engspec § fs-10 create (plan.v3 revised)` — delegates to `open(path, O_CREAT | O_RDWR | O_TRUNC, mode)`.

**Step 1.1 — delegate to open**
→ Frame 1a: `Filesystem.open("/a", flags=O_CREAT | O_RDWR | O_TRUNC, mode=0o644)`.

**Step 1.2 — not-exists path**
Cite `fs.engspec § fs-10 open § Postconditions`:
- Resolve path fails with `NotFound` → take the creation branch.

**Step 1.3 — resolve parent**
Cite `fs.engspec § fs-5`: `_resolve_parent("/a")` → `(parent_inode=1, new_name="a")`.

**Step 1.4 — insert node**
Cite `nodes.engspec § insert (plan.v3 revised)`: `nodes.insert(conn, kind="file", mode, uid=0, gid=0, now_ns=t)` → returns a new inode `N`, creates a row in `nodes` with `nlink=1`.

Bind `inode_a = N`.

**Step 1.5 — insert entry**
Cite `entries.engspec § insert`: `entries.insert(conn, parent=1, name="a", inode=N)` → new row `(1, "a", N)` in `entries`.

Bind `entry_a = (1, "a", N)`.

**Step 1.6 — open a fd**
Cite `fdtable.engspec § FdTable.open`: returns fd (say 1), recorded with `(inode=N, flags=O_CREAT|O_RDWR|O_TRUNC, uid=0, gid=0)`.

### Frame 2: as_root.write(fd, b"content", offset=0)

**Cite** — `fs.engspec § fs-10 write`.

**Step 2.1 — access check**
Fd flags include O_RDWR → write permitted.

**Step 2.2 — write to blobs**
Cite `blobs.engspec § write_range`: inserts a blob row for `(inode=N, chunk_id=0, data=b"content")`. Returns new file_size = 7.

**Step 2.3 — update size**
`nodes.update_size(conn, N, 7, now, now)` → nodes row for N has `size=7`.

### Frame 3: as_root.close_fd(fd)

**Cite** — `fs.engspec § fs-10 close_fd`. Removes fd from table, releases any locks (none here), may-GC (but nlink=1 > 0, skip).

### Frame 4: as_root.link("/a", "/b")

**Call** — `Filesystem.link(src="/a", dst="/b")`
**Cite** — `fs.engspec § fs-11 (plan.v3 revised)` — unblocked by the schema split.

**Step 4.1 — write-guard**
`_require_writable()` passes; fs is read-write.

**Step 4.2 — resolve src**
Cite `fs.engspec § fs-4 _resolve_path`: walks from root, consumes component `"a"` via `entries.get(conn, 1, "a") → entry(1, "a", N)`. Dereferences to inode `N` (no symlink follow needed; kind is "file").

Bind `src_inode = N`.

**Step 4.3 — kind check**
Cite `fs.engspec § fs-11`: reject if `src_node.kind == "dir"`. `nodes.get(N).kind = "file"` ≠ "dir" ✓.

**Step 4.4 — resolve parent for dst**
`_resolve_parent("/b")` → `(parent_inode=1, name="b")`.

Cite `entries.engspec § get`: check if `(1, "b")` exists. It does NOT (we haven't inserted it). Caller proceeds.

**Step 4.5 — insert entry**
Cite `entries.engspec § insert`: `entries.insert(conn, 1, "b", N)`. New row `(1, "b", N)` in entries.

Bind `entry_b = (1, "b", N)`.

**Step 4.6 — bump nlink**
Cite `nodes.engspec § change_nlink`: `nodes.change_nlink(conn, N, +1, now)`. Returns new nlink = 2.

Bind `nlink_after_link = 2`.

**Step 4.7 — bump parent mtime**
`nodes.update_times(conn, 1, mtime_ns=now, ctime_ns=now)`.

### Frame 5: as_root.stat("/a")

**Cite** — `fs.engspec § fs-9 stat`.

**Step 5.1 — resolve path**
Walk via `entries.get(conn, 1, "a")` → entry → inode N.

**Step 5.2 — fetch node row**
`nodes.get(conn, N)` returns NodeRow with inode=N, kind=file, nlink=2, size=7, ...

**Step 5.3 — construct Stat**
Returns `Stat(kind="file", size=7, mode=0o644, uid=0, gid=0, ..., nlink=2, inode=N)`.

### Frame 6: as_root.stat("/b")

**Cite** — same path, but resolve via `entries.get(conn, 1, "b")` → entry → inode N (same N!).

**Step 6.2 — fetch node row**
`nodes.get(conn, N)` — same row as Frame 5. Same fields. Same inode N.

**Step 6.3 — construct Stat**
Returns `Stat(..., nlink=2, inode=N)` — identical to Frame 5's return.

### Frame 7: as_root.open("/b", flags=0)

**Cite** — `fs.engspec § fs-10 open`.

**Step 7.1 — resolve path**
`entries.get(conn, 1, "b") → inode N`.

**Step 7.2 — fd table**
`FdTable.open(inode=N, flags=O_RDONLY, uid=0, gid=0)` → returns fd.

### Frame 8: as_root.read(fd, size=7, offset=0)

**Cite** — `fs.engspec § fs-10 read`.

**Step 8.1 — access check**
Flags are O_RDONLY → R access allowed.

**Step 8.2 — blob read**
Cite `blobs.engspec § read_range`: reads chunk 0 for inode N. Returns the bytes at offset 0, size 7 = `b"content"`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|------|-----------|-------------|-----------|
| LHS (1st) | `as_root.stat("/a").inode` | `N` | Frame 5 Step 5.3 |
| RHS (1st) | `as_root.stat("/b").inode` | `N` | Frame 6 Step 6.3 |
| Op | `==` | True (both equal N) | same node |
| LHS (2nd) | `as_root.stat("/a").nlink` | `2` | Frame 5 |
| RHS (2nd) | `2` | `2` | literal |
| Op | `==` | True | |
| LHS (3rd) | `as_root.stat("/b").nlink` | `2` | Frame 6 |
| RHS (3rd) | `2` | `2` | literal |
| Op | `==` | True | |
| LHS (4th) | `as_root.read(fd, 7, 0)` | `b"content"` | Frame 8 Step 8.2 |
| RHS (4th) | `b"content"` | `b"content"` | literal |
| Op | `==` | True | bytes equality |

## Verdict: PASS

- Every assertion derives from the combined specs of `fs.link`, `entries.insert`, `nodes.change_nlink`, and the dereferencing behavior of `_resolve_path` (which always goes through `entries.get` → `nodes.get`).
- The critical invariant — that `stat("/a").inode == stat("/b").inode` — falls out of the schema change: both paths resolve through the `entries` table to the same node inode.
- `nlink == 2` is the direct postcondition of `nodes.change_nlink(N, +1)` called exactly once during the link step.
- Content sharing (`read "/b"` returns `b"content"` written via `"/a"`) is a consequence of `blobs` keying on inode only — and both paths map to the same inode.
- No underdetermined steps. The trace predicts PASS at runtime.
