# sqlite-fs

A Linux FUSE filesystem whose entire state — directory tree, file contents, xattrs, symlinks, metadata — lives in a single SQLite database. Mounted, it behaves like POSIX. Unmounted, it is a portable `.db` file you can back up, ship, and inspect with `sqlite3`.

**Status: pre-alpha.** Development is driven by the [engspec methodology](https://github.com/misko/engspec_code): English-first specification, traces before code, regeneration from specs. See `idea.md` and `plan.md` for the v1 scope and design.

## Install

Library only (no FUSE, no mount CLI):

```bash
pip install -e .
```

With the FUSE adapter (requires `libfuse3` + `pkg-config` installed on the system):

```bash
pip install -e ".[fuse]"
```

Development (tests, but no FUSE — fuse tests auto-skip):

```bash
pip install -e ".[dev]"
```

Everything:

```bash
pip install -e ".[all]"
```

## Usage (v1, when it lands)

```bash
# Create a filesystem
sqlite-fs mkfs /tmp/store.db

# Mount it
sqlite-fs mount /tmp/store.db /mnt/store

# ... do normal POSIX things under /mnt/store ...

# Unmount
sqlite-fs umount /mnt/store

# Check integrity (works without mounting)
sqlite-fs fsck /tmp/store.db
```

## Design

- **Storage:** SQLite single file, WAL mode, `synchronous=FULL`. Power loss must neither corrupt the DB nor lose committed writes.
- **Layers:** pure-Python `sqlite_fs.Filesystem` library underneath a thin `pyfuse3` adapter. The library is trace-complete; the adapter is exercised through real mounts.
- **Content:** chunked blobs (64 KiB default) so partial writes don't rewrite the whole file.
- **Locking:** all three advisory flavors coexist — POSIX (`fcntl F_SETLK`), OFD (`F_OFD_SETLK`), BSD (`flock`).
- **Hard links** across directories, full `nlink` tracking, inode GC when `nlink == 0` AND no open fds reference it.
- **v1 does NOT include:** full-text search (v2), code symbol indexing (v3), `allow_other` mounts, mandatory locks, non-UTF-8 path components, Windows/macOS.

## Development

v1 is still being specified. Stages so far:

- `idea.md` — v1 scope and API sketch
- `plan.md` — module layout, test strategy, edge cases, ambiguities
- `package/` — engspec package (to be created at stage 3)
- `src/sqlite_fs/` — source code (to be regenerated at stage 7 from the engspec package)
- `tests/` — tests (likewise regenerated)

See `~/gits/engspec_code/` for the methodology (`engspec_prompt.md`, `engspec_trace_prompt.md`, etc.) and `~/gits/engspec_code/tests/json_pointer/` for a worked end-to-end example.
