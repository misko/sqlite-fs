# sqlite-fs: English-First Implementation Plan

## One-line intent

A FUSE filesystem where every file, directory, and its full-text + code-symbol
index live inside a single SQLite database. Mounted, it behaves like any
POSIX directory for notes, code, and results. Unmounted, it is a portable
`.db` file you can `sqlite3`, back up, and ship.

## The build order: **tests are the specification**

The engspec methodology says each function's `.engspec` is shorter than its
code but precise enough to regenerate it. We take this one step further:
**we write the test engspecs before the implementation engspecs**. The test
engspecs' assertion blocks (per `engspec_format.md`, every assertion is a
verbatim code block) become the contract every implementation spec must
satisfy.

So the workflow for sqlite-fs is:

1. Write `tests/*.engspec` — enumerate every observable behavior as code-block
   assertions. Validate each via engspec regeneration (3/3).
2. Adversarial debate (`engspec_tester`) on the test specs. Gaps here are
   cheap; gaps after implementation are expensive.
3. Derive module `.engspec` files whose postconditions satisfy the union of
   test assertions. No implementation spec may be validated without a test
   that exercises it.
4. Regenerate code from module specs. The generated tests (from step 1)
   must pass the generated implementation (from step 3) without edits.
5. Any bug found later becomes a new test assertion in an existing test
   `.engspec` first, then a spec update, then code.

The rule: **no code gets written whose behavior isn't already pinned down
in a test engspec's assertion block.**

---

## The test engspecs

Ten test-spec files. For each one I've listed the specific assertions — these
are the ones I'd actually write as code blocks in the `.engspec`. They are
not exhaustive but they are the ones that would catch the mistakes that are
easy to make.

### `tests/conftest.engspec` — fixtures

Fixtures are the test infrastructure: if they're weak, every test below is
weak. Each fixture gets a `##` section.

- `fresh_fs(tmp_path)` → yields `(mount_point, db_path, daemon_handle)`.
  Creates empty `.db`, runs `mkfs`, spawns daemon, waits for mount, yields,
  then unmounts and checks `PRAGMA integrity_check == 'ok'` as a
  per-test invariant.
- `populated_fs(fresh_fs)` → same, pre-seeded with a known tree
  (`/notes/a.md`, `/code/hello.py`, `/results/run1.json`). Exact contents
  embedded as code blocks so regeneration reproduces byte-for-byte.
- `crashable_fs(fresh_fs)` → daemon can be `SIGKILL`ed and remounted in-test.
- `sql(db_path)` → returns a read-only connection for invariant queries.

### `tests/posix_basic.engspec` — round-trips and counts

The boring tests that fail in surprising ways:

- `write(b"hello") + close + open + read()` returns `b"hello"`, exactly.
- After write of N bytes, `stat(path).st_size == N` and
  `sql.execute("SELECT length(data) FROM blobs WHERE inode = ?")` returns N.
- `readdir` yields every `creat`ed name exactly once; no duplicates after
  `rename`; `.` and `..` are synthesized by the kernel, not stored.
- `truncate(path, 0)` → `st_size == 0` AND `SELECT count(*) FROM fts WHERE
  inode = ?` returns 0 (index cleared).
- `truncate(path, size + 100)` zero-fills: read of the gap returns `b"\x00" * 100`.
- `pwrite(fd, b"X", offset=size+10)` creates a sparse-looking hole; we don't
  implement real sparseness, so the gap is materialized as zero bytes and
  `st_size == offset + 1`. (Stated as a negative boundary.)
- `O_APPEND` write from two fds lands at the new end each time (serialized).
- Empty-file creation: `creat(path)` then `stat(path).st_size == 0` and
  `SELECT count(*) FROM blobs WHERE inode = ?` returns 1 (row with empty blob,
  not missing — simpler invariant).
- Zero-length read past EOF returns `b""`, not an error.

### `tests/posix_rename.engspec` — the place every fs has bugs

Rename is where observable atomicity matters most. Every one of these is a
distinct test function:

- `rename(a, b)` where `b` does not exist: `a` gone, `b` has `a`'s content
  AND `a`'s inode number (editor-atomicity depends on this).
- `rename(a, b)` where `b` exists as file: `b` is overwritten, the old `b`
  inode has zero `nlink` and is garbage-collected; `SELECT count(*) FROM
  nodes WHERE inode = old_b` returns 0.
- `rename(a, b)` where `a == b`: returns 0, no change, no FTS churn
  (`SELECT count(*) FROM fts_log` unchanged if we keep one).
- `rename(a, b)` where `b` is a non-empty dir: raises `OSError(ENOTEMPTY)`.
- `rename(dir, dir/child)`: raises `OSError(EINVAL)` — can't move a dir
  into its own subtree. Assertion walks parent chain before allowing.
- Cross-directory rename: mtime of both parent dirs updated; mtime of the
  renamed file itself unchanged.
- Editor atomic-save dance: `write /tmp.swp; rename /tmp.swp /real` leaves
  `/real` with new content and a stable path visible to `inotify`-equivalent
  consumers (for us, the FTS row of `/real` reflects new content).

### `tests/posix_permissions.engspec` — modes, owners, timestamps

- `chmod(path, 0o644)` then `stat(path).st_mode & 0o777 == 0o644`.
- `chown` updates `uid`/`gid` columns; running as non-root, changing uid
  to someone else raises `OSError(EPERM)`.
- `utimes(path, (atime, mtime))` sets both; `ctime` updates to "now" but
  can't be set directly.
- `umask` is applied by the kernel before reaching us — we test that we
  store what we receive and don't re-apply.
- New file inherits `gid` from the parent directory iff the parent has the
  setgid bit. (Linux semantics. Explicit test.)

### `tests/invariants.engspec` — property tests over random op sequences

The assertions here are invariants, not single-case outputs. Each test
generates a random sequence of ops (via `hypothesis` stateful testing) then
checks:

```python
# no directory is its own ancestor
assert sql.execute("""
    WITH RECURSIVE walk(inode, ancestor) AS (
      SELECT inode, parent FROM nodes WHERE parent IS NOT NULL
      UNION ALL
      SELECT w.inode, n.parent FROM walk w JOIN nodes n ON n.inode = w.ancestor
      WHERE n.parent IS NOT NULL
    )
    SELECT count(*) FROM walk WHERE inode = ancestor
""").fetchone()[0] == 0
```

```python
# every file's st_size equals its blob length
mismatches = sql.execute("""
    SELECT n.inode FROM nodes n
    LEFT JOIN blobs b ON b.inode = n.inode
    WHERE n.kind = 'file' AND n.size != COALESCE(length(b.data), 0)
""").fetchall()
assert mismatches == []
```

```python
# FTS has exactly one row per text file, zero per non-text
fts_coverage = sql.execute("""
    SELECT n.inode, n.is_text, (f.rowid IS NOT NULL) AS has_fts
    FROM nodes n LEFT JOIN fts f ON f.rowid = n.inode
    WHERE n.kind = 'file'
""").fetchall()
for inode, is_text, has_fts in fts_coverage:
    assert bool(is_text) == bool(has_fts)
```

```python
# st_nlink on a directory == 2 + number of subdirectories
for dir_inode in all_dir_inodes:
    subdirs = sql.execute(
        "SELECT count(*) FROM nodes WHERE parent = ? AND kind = 'dir'",
        (dir_inode,)
    ).fetchone()[0]
    assert stat_nlink(dir_inode) == 2 + subdirs
```

### `tests/errors.engspec` — every errno we promise to return

One test per errno, matched against a single operation each:

| Op | Condition | Errno |
|---|---|---|
| `open(x)` | x does not exist, no `O_CREAT` | `ENOENT` |
| `open(x, O_CREAT\|O_EXCL)` | x exists | `EEXIST` |
| `mkdir(x)` | x exists | `EEXIST` |
| `rmdir(x)` | x is non-empty | `ENOTEMPTY` |
| `rmdir(x)` | x is a file | `ENOTDIR` |
| `unlink(x)` | x is a directory | `EISDIR` |
| `read(dir_fd)` | fd is a directory | `EISDIR` |
| `creat(a/b)` | a does not exist | `ENOENT` |
| `creat(name)` | `len(name) > 255` | `ENAMETOOLONG` |
| `rename(a, a/sub)` | destination is descendant | `EINVAL` |

Each assertion is `with pytest.raises(OSError) as e: op(); assert e.value.errno == errno.X`.

### `tests/search.engspec` — the search contract

- Write `"hello world"` to `/notes/a.md`; `search("hello")` returns exactly
  `[("/notes/a.md", <rank>)]`.
- `unlink("/notes/a.md")` → `search("hello")` returns `[]`.
- `rename("/notes/a.md", "/archive/a.md")` → `search("hello")` returns
  `[("/archive/a.md", ...)]` (FTS row survives the rename).
- Overwrite content from `"hello"` to `"goodbye"` → `search("hello")` is
  empty, `search("goodbye")` returns the path.
- Write a PNG header (`b"\x89PNG\r\n\x1a\n..."`) to `/img.png` →
  `SELECT count(*) FROM fts WHERE rowid = inode(/img.png)` returns 0
  (binary not indexed), no error raised.
- Unicode: write `"こんにちは"` → `search("こんにちは")` returns the path.
- Query syntax: `search("lang:python def foo")` returns only `.py` files
  containing `def foo` (tests the query grammar, not just free text).

### `tests/symbols.engspec` — code-aware indexing

- Write `def foo(): pass\nclass Bar: pass\n` to `/a.py`; after close,
  `SELECT name, kind FROM symbols WHERE inode = ?` returns
  `[("foo", "function"), ("Bar", "class")]`.
- Edit to `def baz(): pass` → symbols table now has only `baz`; `foo` and
  `Bar` gone.
- Write Python with a syntax error → file indexed as text (FTS row exists),
  symbols table has zero rows for that inode, no error returned to caller.
- Write Markdown with headings `# Title\n## Subtitle` → symbols table has
  `[("Title", "heading"), ("Subtitle", "heading")]`.
- Unlink file → symbols gone.
- Rename file → symbol rows retain inode, `JOIN nodes` resolves to new path.
- Language we don't support (`.rb`) → FTS yes, symbols no, no error.

### `tests/interop.engspec` — real tools on the mount

These are slow but non-negotiable. Each asserts an observable outcome of
running a real tool inside the mount:

- `subprocess.run(["git", "init"])` returns 0; `.git/` is a real directory
  with expected files; `git add . && git commit -m x` succeeds.
- `subprocess.run(["rsync", "-a", external_dir, mount])` — after, every file
  in `external_dir` exists in `mount` with the same `st_mode` and `st_mtime`
  (within 1s tolerance for FS granularity).
- `subprocess.run(["tar", "-cf", "out.tar", "-C", mount, "."])` followed by
  extraction to an `ext4` tmpdir yields byte-identical content.
- `subprocess.run(["grep", "-r", "needle", mount])` finds lines written
  earlier in the test. (This is a sanity check that we look like a real fs
  to `grep`.)
- Vim atomic-save: simulate vim's `write-to-.swp → rename` sequence and
  assert the final file has the new content and the old swap file is gone.

### `tests/crash_safety.engspec` — SIGKILL the daemon

Uses the `crashable_fs` fixture. Each test writes, SIGKILLs the daemon,
remounts from the same `.db`, then asserts:

- `PRAGMA integrity_check` returns `ok`.
- Every file either has its pre-kill content or its post-kill content, never
  a mix (per-inode atomicity — guaranteed by committing each `write` in a
  single transaction).
- FTS index agrees with content (run the invariant from `invariants.engspec`).
- Re-mounting is idempotent: mounting twice in sequence leaves no stale
  WAL or SHM lock state.

### `tests/cli.engspec` — the user-facing commands

- `sqlite-fs mkfs foo.db` creates a valid empty filesystem (mountable).
- `sqlite-fs mount foo.db /mnt` mounts and daemonizes; a PID file lives at
  a documented path; `sqlite-fs umount /mnt` cleans it up.
- `sqlite-fs search -d foo.db "hello"` returns the same rows as the in-fs
  search path, without needing a live mount (read-only attach).
- `sqlite-fs fsck foo.db` reports zero issues on a freshly-made DB; reports
  a specific issue on a DB we deliberately corrupt (delete a row from
  `nodes` but not its `blobs` entry).
- `sqlite-fs export foo.db /tmp/out/` recreates the tree as real files on
  the host FS, with correct modes and mtimes.

---

## Explicit test-level negative boundaries

Things we deliberately do NOT test in v1, stated as negative boundaries
so reviewers don't add them silently:

- **Performance.** No latency or throughput assertions — they are flaky
  in CI and are not correctness.
- **`mmap`.** `pyfuse3` supports it weakly; we document "mmap may silently
  return stale bytes after concurrent writes" rather than test against it.
- **Multi-writer daemons.** One daemon per DB. Two daemons on the same DB
  is out of scope; behavior is undefined.
- **Non-Linux.** macOS FUSE behaves differently enough that we defer it.
- **File locks (`flock`, `fcntl`).** Not implemented; tests that rely on
  them (e.g., SQLite-inside-sqlite-fs) are marked expected-fail.
- **Hard links across directories, after v1.** Allowed, tested in
  `posix_basic`, but refcounting edge cases (link count going to 0 while
  an fd is still open) are v2.

---

## Architecture (unchanged from v1 of the plan, repeated for completeness)

One SQLite file holds three layers:

1. **Structure** — `nodes(inode, parent, name, kind, mode, uid, gid, size,
   atime, mtime, ctime, is_text)`.
2. **Content** — `blobs(inode, data)` — one blob per inode for v1.
3. **Index** — `fts` (FTS5 virtual table keyed by inode) and
   `symbols(inode, name, kind, line, col)`.

Triggers keep FTS in sync with `blobs` on every write. Symbols are
refreshed on file close for recognized extensions. WAL mode, `synchronous
= NORMAL`. Single writer (the daemon), many readers (the search CLI
attaches read-only).

## Implementation engspecs (derived from tests, not vice versa)

| Spec | Tests it must satisfy |
|---|---|
| `schema.engspec`    | `invariants`, `crash_safety`, `cli` (mkfs/fsck) |
| `errors.engspec`    | `errors` (all of it) |
| `nodes.engspec`     | `posix_basic`, `posix_rename`, `posix_permissions` |
| `blobs.engspec`     | `posix_basic` (write/read/truncate), `crash_safety` |
| `fts.engspec`       | `search`, `invariants` (fts-content sync) |
| `symbols.engspec`   | `symbols` |
| `fuse_ops.engspec`  | `interop` (what real tools observe) |
| `search.engspec`    | `search` (query grammar) |
| `cli.engspec`       | `cli`, `crash_safety` (remount) |

If a test assertion is not covered by any implementation spec's
postconditions, that's a SpecGap and the implementation spec is incomplete.
If an implementation spec's postcondition is not exercised by any test
assertion, that's dead prose — strip it or add the test.

## Key design decisions

1. **One blob per file, not chunked.** SQLite handles GB-scale blobs;
   revisit if tests/large-file emerge (they don't, v1).
2. **Python + `pyfuse3`**, not Rust. Matches workload; releases GIL on syscalls.
3. **FTS5 triggers**, not explicit reindex. Keeps the `invariants` test green.
4. **Tree-sitter for symbols.** One grammar per supported language.
5. **Search via CLI + xattrs**, not `.fts/` virtual dir — collides with
   `mkdir` namespace and confuses `rsync`.
6. **Empty-blob row exists for empty files** — simpler invariant than
   "blob row optional for zero-size files."

## Open questions — decide before writing test specs

- **xattr support?** If yes, add `tests/xattr.engspec` now rather than
  bolt on later.
- **Hard-link refcount tests past v1?** If no, add negative boundary.
- **fsck scope — how many corruption classes?** Each is a test case.
- **Do we test behavior under `EINTR` from SQLite?** `SQLITE_BUSY` is
  relevant under WAL; probably a single test in `crash_safety`.

## First concrete deliverables

1. `tests/conftest.engspec` validated 3/3 — fixtures are the foundation.
2. `tests/posix_basic.engspec` validated 3/3.
3. `tests/errors.engspec` validated 3/3.
4. Adversarial debate on those three.
5. Only then: `schema.engspec`, `errors.engspec` implementation specs.
