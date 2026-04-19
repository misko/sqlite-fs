# Plan v3: sqlite-fs v1

*Stage 6 finding-driven revision. Two concrete engspec updates surfaced by runtime and by implementation attempts. Everything else from plan.v2 stands.*

## Changes from v2

### 1. `create()` returns `O_RDWR`, not `O_WRONLY`

**Finding:** writing `test_blobs` and running pytest produced 10 failures on the shape `fd = fs.create("/f"); fs.write(fd, ...); fs.read(fd, ...)`. My regenerated `fs.open` correctly refused reads on an `O_WRONLY` fd, per the spec — but the spec said `create()` delegates to `open(..., O_CREAT | O_WRONLY | O_TRUNC, ...)`. POSIX `creat(2)` literally does that, but a Python-level ergonomic API wants the fd to be usable for both read and write.

**Resolution:** `fs.engspec § fs-10` revised: `create(path, mode, flags)` delegates to `open(path, flags | O_CREAT | O_RDWR | O_TRUNC, mode)`. Matches what every test expects.

**Methodology note:** this is exactly the signal Stage 6 pytest is supposed to produce — a gap the trace didn't catch because the trace stopped at spec-derivation and didn't exercise the "does the caller actually use the returned fd both ways" question. Worth recording: traces are strong for logic but weaker for "does the public API shape match caller expectations." Regenerated-code-plus-real-tests is the mechanism that closes this last gap.

### 2. Directory entries split into their own table — enables hard links

**Finding:** `fs.link()` can't work against the v1 schema. Two directory entries pointing at the same inode is a fundamental hard-link semantic, but the v1 `nodes` table keys on `(parent, name)` as part of the *same* row as the inode. We can't have two rows with the same inode (it's the PK).

**Resolution:** split the schema into two tables.

New DDL (replacing the v1 `nodes` table definition):

```sql
CREATE TABLE nodes (
    inode INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'symlink')),
    mode INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    atime_ns INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    nlink INTEGER NOT NULL
);

CREATE TABLE entries (
    parent INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    name TEXT NOT NULL,
    inode INTEGER NOT NULL REFERENCES nodes(inode),
    PRIMARY KEY (parent, name)
);
```

- **Nodes** hold file/dir/symlink metadata. One row per inode.
- **Entries** are directory entries. Multiple entries per inode = hard links. Root is a node with no entries pointing at it.
- `entries.parent → nodes.inode ON DELETE CASCADE`: removing a directory removes its entries.
- `entries.inode → nodes.inode` (no cascade): a node is only deleted when `nlink==0 && open_fd_count==0`; the GC path does the deletion explicitly.

**Impact on impl engspecs:**

- `schema.engspec` — new DDL block (see above).
- `nodes.engspec` — `NodeRow` drops `parent` and `name` fields. Signature changes:
  - `insert(conn, kind, mode, uid, gid, now_ns) -> int` — no parent/name.
  - Remove `get_child`, `list_children`, `rename_entry`, `count_children` — these move to the new `entries` module.
  - `ancestry(inode)` now walks via `entries.parent`.
- **NEW** `entries.engspec`:
  - `insert(conn, parent, name, inode)`
  - `get(conn, parent, name) -> EntryRow`
  - `delete(conn, parent, name)`
  - `list(conn, parent) -> list[EntryRow]` (ordered by name)
  - `count(conn, parent, kind=None) -> int` (with JOIN for kind filter)
  - `rename(conn, old_parent, old_name, new_parent, new_name)`
- `fs.engspec § fs-11 link()` — unblocked. Inserts a new entry for the existing inode, bumps nlink.
- `fs.engspec § fs-6/7/8` (mkdir, rmdir, readdir) — rewritten to use `entries` module.
- `types.engspec` — add `EntryRow` dataclass.

**Impact on impl code:**
- New `src/sqlite_fs/entries.py`.
- Rewrite of `src/sqlite_fs/nodes.py` (most functions change shape).
- Updates throughout `src/sqlite_fs/fs.py` (every orchestrator method that reads the tree).

**Impact on test engspecs:** none — public API unchanged. Existing tests pass as-is.

**Impact on test code:** none — same reason. Plus `test_hardlinks` (13 tests) now exercises working code, no longer blocked.

### 3. Minor: `exists()` swallows `NotADirectory`

**Finding:** `fs.exists("/f/sub")` where `/f` is a file previously raised `NotADirectory`, but callers use `exists()` as a boolean. Current code swallows `NotFound` only.

**Resolution:** `fs.engspec § fs-9 exists()` — catch `(NotFound, NotADirectory)`. Already applied in the regenerated code; engspec update is documentation, not a behavior change.

## What stays the same

Everything else from plan.v2. The four-table storage schema stays except for the `nodes` / `entries` split above. Blobs, xattrs, symlinks tables are unchanged — they still key on inode.

## Trace update

One new trace exercises `test_link_creates_hard_link` against the revised `fs.link` + `entries.insert` + `nodes.change_nlink` spec chain. Produced at `package/traces/test_link_creates_hard_link.trace.md` before regenerating code, so we confirm the spec-alone derivation predicts PASS.

## Deliverables for this revision

1. `plan.v3.md` — this document.
2. `package/specs/src/schema.py.engspec` — revised DDL block.
3. `package/specs/src/entries.py.engspec` — new module.
4. `package/specs/src/nodes.py.engspec` — reshape.
5. `package/specs/src/fs.py.engspec` — link + read-the-tree updates.
6. `package/specs/src/types.py.engspec` — add `EntryRow`.
7. `package/traces/test_link_creates_hard_link.trace.md` — new trace.
8. Regenerated: `schema.py`, `entries.py` (new), `nodes.py`, `fs.py`, `types.py`.
9. pytest green on the previously-skipped `test_hardlinks.py` (13 tests).
