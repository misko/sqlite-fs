import os
import sqlite3
import time
from contextlib import contextmanager

from sqlite_fs import blobs, entries, nodes
from sqlite_fs import xattrs as xattrs_mod
from sqlite_fs import symlinks as symlinks_mod
from sqlite_fs.errors import (
    AlreadyExists,
    BadFileDescriptor,
    DirectoryNotEmpty,
    FilesystemError,
    InvalidArgument,
    IsADirectory,
    NotADirectory,
    NotFound,
    PermissionDenied,
    ReadOnlyFilesystem,
    SymlinkLoop,
)
from sqlite_fs.fdtable import FdTable
from sqlite_fs.locks import LockManager
from sqlite_fs.paths import parse_path
from sqlite_fs.perms import Access, check_access, require_access
from sqlite_fs.schema import MAXSYMLINKS, ROOT_INODE, apply_pragmas, load_chunk_size
from sqlite_fs.types import DirEntry, Stat
from sqlite_fs.watch import Event, Watcher


class Filesystem:
    def __init__(self, conn, *, readonly, uid, gid, sync_mode="full",
                 checkpoint_interval_ms=None):
        self._conn = conn
        self._readonly = readonly
        self._uid = uid
        self._gid = gid
        apply_pragmas(conn, sync_mode=sync_mode)
        self._chunk_size_val = load_chunk_size(conn)
        self._fd_table = FdTable()
        self._lock_mgr = LockManager()
        self._watchers: set = set()           # plan.v4
        self._sync_mode = sync_mode           # plan.v5
        self._path_cache: dict = {}           # plan.v5: inode -> path cache

        # plan.v6: optional group-commit timer. If checkpoint_interval_ms
        # is set, a background thread runs PRAGMA wal_checkpoint(PASSIVE)
        # every N ms. Bounds the "data loss window on power loss" by time,
        # not by WAL page count. See plan.v6.md.
        self._checkpoint_interval_ms = checkpoint_interval_ms
        self._checkpoint_stop = None
        self._checkpoint_thread = None
        if (checkpoint_interval_ms is not None
                and not readonly):
            self._start_checkpoint_thread(checkpoint_interval_ms)

    # --- plan.v4: Event emission protocol ---

    def _path_of_inode(self, inode):
        """Walk entries.parent up to root. Cached by inode (plan.v5).
        Cache is invalidated on any rename; for hard-linked inodes,
        returns one of the paths (arbitrary). None on detached inode."""
        if inode == ROOT_INODE:
            return "/"
        cached = self._path_cache.get(inode)
        if cached is not None:
            return cached
        parts = []
        cur = inode
        while cur != ROOT_INODE:
            row = self._conn.execute(
                "SELECT parent, name FROM entries WHERE inode = ? LIMIT 1",
                (cur,),
            ).fetchone()
            if row is None:
                return None
            parts.append(row[1])
            cur = row[0]
        path = "/" + "/".join(reversed(parts))
        self._path_cache[inode] = path
        return path

    def _emit(self, event):
        """Deliver to every matching watcher. Called after commit only."""
        for watcher in list(self._watchers):
            if watcher._matches(event):
                watcher._enqueue(event)

    def watch(self, path, *, recursive=False):
        """Register a Watcher. Caller must have R access on `path` (or be
        root). Uses the standard path-resolution machinery to validate."""
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        if node.kind != "dir":
            raise NotADirectory(f"{path!r} is not a directory")
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, Access.R)
        return Watcher(self, path, recursive=recursive)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self._checkpoint_stop is not None:
            self._checkpoint_stop.set()
            if self._checkpoint_thread is not None:
                self._checkpoint_thread.join(timeout=2)
        try:
            self._conn.commit()
            if not self._readonly:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        finally:
            self._conn.close()

    def _start_checkpoint_thread(self, interval_ms):
        """plan.v6: group-commit timer. Runs PRAGMA wal_checkpoint(PASSIVE)
        every interval_ms. Bounds the power-loss data-loss window in time,
        not just in WAL page count."""
        import threading
        import sqlite3 as _sq

        # The checkpoint runs on a second connection to the same DB so
        # that we don't contend for the main connection's write lock.
        db_path = self._conn.execute("PRAGMA database_list").fetchone()[2]
        self._checkpoint_stop = threading.Event()

        def loop():
            # Dedicated connection — SQLite connections are not safe to
            # share across threads by default.
            ckpt_conn = _sq.connect(db_path)
            try:
                ckpt_conn.execute("PRAGMA busy_timeout = 1000")
                while not self._checkpoint_stop.wait(interval_ms / 1000.0):
                    try:
                        ckpt_conn.execute(
                            "PRAGMA wal_checkpoint(PASSIVE)"
                        ).fetchone()
                    except _sq.OperationalError:
                        # Busy — next tick will retry.
                        pass
            finally:
                ckpt_conn.close()

        self._checkpoint_thread = threading.Thread(
            target=loop, daemon=True, name="sqlite-fs-checkpoint",
        )
        self._checkpoint_thread.start()

    @contextmanager
    def as_user(self, uid, gid):
        prev = (self._uid, self._gid)
        self._uid, self._gid = uid, gid
        try:
            yield self
        finally:
            self._uid, self._gid = prev

    # --- private helpers ---

    def _require_writable(self):
        if self._readonly:
            raise ReadOnlyFilesystem("filesystem is read-only")

    def _resolve_path(self, path, *, follow_final_symlink=True,
                      symlinks_traversed=0):
        components = parse_path(path)
        cur = ROOT_INODE
        for i, name in enumerate(components):
            cur_node = nodes.get(self._conn, cur)
            if cur_node.kind != "dir":
                raise NotADirectory(f"{name!r} has non-dir ancestor")
            require_access(cur_node.mode, cur_node.uid, cur_node.gid,
                           self._uid, self._gid, Access.X)
            entry = entries.get(self._conn, cur, name)
            child = nodes.get(self._conn, entry.inode)
            is_final = (i == len(components) - 1)
            if child.kind == "symlink" and (not is_final or follow_final_symlink):
                if symlinks_traversed >= MAXSYMLINKS:
                    raise SymlinkLoop(f"too many symlinks resolving {path!r}")
                target_bytes = symlinks_mod.get(self._conn, child.inode)
                try:
                    target = target_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raise InvalidArgument(
                        f"symlink target not UTF-8 resolvable: {target_bytes!r}"
                    )
                if not target.startswith("/"):
                    parent_components = components[:i]
                    target = "/" + "/".join(parent_components + [target])
                remaining = components[i + 1:]
                rewritten = target + ("/" + "/".join(remaining) if remaining else "")
                return self._resolve_path(
                    rewritten,
                    follow_final_symlink=follow_final_symlink,
                    symlinks_traversed=symlinks_traversed + 1,
                )
            cur = child.inode
        return cur

    def _resolve_parent(self, path):
        components = parse_path(path)
        if not components:
            raise InvalidArgument("cannot operate on the root")
        *parent_components, new_name = components
        parent_path = "/" + "/".join(parent_components)
        parent_inode = self._resolve_path(parent_path)
        parent = nodes.get(self._conn, parent_inode)
        if parent.kind != "dir":
            raise NotADirectory(f"parent {parent_path!r} is not a directory")
        require_access(parent.mode, parent.uid, parent.gid,
                       self._uid, self._gid, Access.W | Access.X)
        return parent_inode, new_name

    def _maybe_gc(self, inode):
        try:
            node = nodes.get(self._conn, inode)
        except NotFound:
            return
        if node.nlink == 0 and self._fd_table.open_count(inode) == 0:
            nodes.delete(self._conn, inode)

    # --- directory ---

    def mkdir(self, path, mode=0o755):
        self._require_writable()
        parent_inode, name = self._resolve_parent(path)
        try:
            entries.get(self._conn, parent_inode, name)
            raise AlreadyExists(f"path exists: {path!r}")
        except NotFound:
            pass
        now = time.time_ns()
        with self._conn:
            new_inode = nodes.insert(
                self._conn, "dir", mode, self._uid, self._gid, now,
            )
            entries.insert(self._conn, parent_inode, name, new_inode)
            nodes.change_nlink(self._conn, parent_inode, +1, now)
            nodes.update_times(self._conn, parent_inode,
                               mtime_ns=now, ctime_ns=now)
        self._emit(Event(
            kind="create", path=path, src_path=None, dst_path=None,
            node_kind="dir", inode=new_inode, timestamp_ns=now,
        ))

    def rmdir(self, path):
        self._require_writable()
        if path == "/":
            raise PermissionDenied("cannot remove root")
        parent_inode, name = self._resolve_parent(path)
        entry = entries.get(self._conn, parent_inode, name)
        node = nodes.get(self._conn, entry.inode)
        if node.kind != "dir":
            raise NotADirectory(f"{path!r} is not a directory")
        if entries.count(self._conn, entry.inode) > 0:
            raise DirectoryNotEmpty(f"directory not empty: {path!r}")
        removed_inode = entry.inode
        now = time.time_ns()
        with self._conn:
            entries.delete(self._conn, parent_inode, name)
            nodes.delete(self._conn, entry.inode)
            nodes.change_nlink(self._conn, parent_inode, -1, now)
            nodes.update_times(self._conn, parent_inode,
                               mtime_ns=now, ctime_ns=now)
        self._path_cache.pop(removed_inode, None)
        self._emit(Event(
            kind="remove", path=path, src_path=None, dst_path=None,
            node_kind="dir", inode=removed_inode, timestamp_ns=now,
        ))

    def readdir(self, path):
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        if node.kind != "dir":
            raise NotADirectory(f"{path!r} is not a directory")
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, Access.R)
        result = []
        for entry in entries.list_(self._conn, inode):
            child = nodes.get(self._conn, entry.inode)
            result.append(
                DirEntry(name=entry.name, kind=child.kind, inode=entry.inode),
            )
        return result

    # --- metadata ---

    def stat(self, path, *, follow_symlinks=True):
        inode = self._resolve_path(path, follow_final_symlink=follow_symlinks)
        node = nodes.get(self._conn, inode)
        return Stat(
            kind=node.kind, size=node.size, mode=node.mode,
            uid=node.uid, gid=node.gid,
            atime_ns=node.atime_ns, mtime_ns=node.mtime_ns,
            ctime_ns=node.ctime_ns, nlink=node.nlink, inode=node.inode,
        )

    def lstat(self, path):
        return self.stat(path, follow_symlinks=False)

    def exists(self, path):
        try:
            self.stat(path)
            return True
        except (NotFound, NotADirectory):
            return False

    def chmod(self, path, mode, *, follow_symlinks=True):
        self._require_writable()
        inode = self._resolve_path(path, follow_final_symlink=follow_symlinks)
        node = nodes.get(self._conn, inode)
        if self._uid != 0 and self._uid != node.uid:
            raise PermissionDenied("only owner or root may chmod")
        now = time.time_ns()
        with self._conn:
            nodes.update_mode_uid_gid(self._conn, inode,
                                      mode=mode & 0o7777, ctime_ns=now)
        self._emit(Event(
            kind="metadata", path=path, src_path=None, dst_path=None,
            node_kind=node.kind, inode=inode, timestamp_ns=now,
        ))

    def chown(self, path, uid, gid, *, follow_symlinks=True):
        self._require_writable()
        inode = self._resolve_path(path, follow_final_symlink=follow_symlinks)
        node = nodes.get(self._conn, inode)
        if self._uid != 0 and (uid != node.uid or gid != node.gid):
            raise PermissionDenied("only root may change owner/group")
        now = time.time_ns()
        with self._conn:
            nodes.update_mode_uid_gid(self._conn, inode,
                                      uid=uid, gid=gid, ctime_ns=now)
        self._emit(Event(
            kind="metadata", path=path, src_path=None, dst_path=None,
            node_kind=node.kind, inode=inode, timestamp_ns=now,
        ))

    def utimes(self, path, atime_ns, mtime_ns, *, follow_symlinks=True):
        self._require_writable()
        inode = self._resolve_path(path, follow_final_symlink=follow_symlinks)
        node = nodes.get(self._conn, inode)
        if self._uid != 0 and self._uid != node.uid:
            raise PermissionDenied("only owner or root may utimes")
        now = time.time_ns()
        with self._conn:
            nodes.update_times(self._conn, inode,
                               atime_ns=atime_ns, mtime_ns=mtime_ns, ctime_ns=now)
        self._emit(Event(
            kind="metadata", path=path, src_path=None, dst_path=None,
            node_kind=node.kind, inode=inode, timestamp_ns=now,
        ))

    # --- file ---

    def create(self, path, mode=0o644, flags=0):
        # plan.v3: create() returns O_RDWR (ergonomic Python API).
        return self.open(path,
                         flags=flags | os.O_CREAT | os.O_RDWR | os.O_TRUNC,
                         mode=mode)

    def open(self, path, flags=0, mode=0o644):
        creat = bool(flags & os.O_CREAT)
        excl = bool(flags & os.O_EXCL)
        trunc = bool(flags & os.O_TRUNC)
        nofollow = bool(flags & os.O_NOFOLLOW)

        try:
            inode = self._resolve_path(path, follow_final_symlink=not nofollow)
            target_exists = True
        except NotFound:
            target_exists = False
            inode = None

        if target_exists:
            if creat and excl:
                raise AlreadyExists(f"path exists: {path!r}")
            if nofollow:
                node_check = nodes.get(self._conn, inode)
                if node_check.kind == "symlink":
                    raise SymlinkLoop(f"O_NOFOLLOW refused: {path!r} is a symlink")
        else:
            if not creat:
                raise NotFound(f"no such file: {path!r}")
            self._require_writable()
            parent_inode, name = self._resolve_parent(path)
            now = time.time_ns()
            with self._conn:
                inode = nodes.insert(
                    self._conn, "file", mode & 0o7777,
                    self._uid, self._gid, now,
                )
                try:
                    entries.insert(self._conn, parent_inode, name, inode)
                except sqlite3.IntegrityError:
                    raise AlreadyExists(f"path exists: {path!r}")
                nodes.update_times(self._conn, parent_inode,
                                   mtime_ns=now, ctime_ns=now)
            # plan.v4: emit create event for the newly-created file.
            self._emit(Event(
                kind="create", path=path, src_path=None, dst_path=None,
                node_kind="file", inode=inode, timestamp_ns=now,
            ))

        node = nodes.get(self._conn, inode)
        access_bits = Access(0)
        access_mode = flags & 0o3
        if access_mode in (os.O_RDONLY, os.O_RDWR):
            access_bits |= Access.R
        if access_mode in (os.O_WRONLY, os.O_RDWR):
            access_bits |= Access.W
        if access_bits == Access(0):
            access_bits = Access.R
        # plan.v3 finding (git add): skip the mode-based access check for
        # newly-created files. The creator is allowed to open with whatever
        # flags they asked for regardless of the mode they applied. This
        # is how `open(O_CREAT|O_RDWR, 0o444)` succeeds on Linux — git
        # writes its object database files that way.
        if target_exists:
            require_access(node.mode, node.uid, node.gid,
                           self._uid, self._gid, access_bits)

        if trunc and target_exists and node.kind == "file":
            self._require_writable()
            now = time.time_ns()
            with self._conn:
                blobs.truncate_to(self._conn, inode,
                                  0, old_size=node.size,
                                  chunk_size=self._chunk_size_val)
                nodes.update_size(self._conn, inode, 0, now, now)

        return self._fd_table.open(inode, flags, self._uid, self._gid)

    def close_fd(self, fd):
        entry = self._fd_table.get(fd)
        inode = entry.inode
        pid = os.getpid()
        self._fd_table.close(fd)
        self._lock_mgr.on_fd_close(inode, fd, pid)
        # plan.v5: only commit a transaction if GC is actually eligible.
        # Previously every close_fd did `with self._conn:` → fsync even
        # for read-only fds, which doubled read-path latency.
        if self._readonly:
            return
        if self._fd_table.open_count(inode) != 0:
            return  # other fds still hold the inode; GC not possible now
        try:
            node = nodes.get(self._conn, inode)
        except NotFound:
            return
        if node.nlink == 0:
            with self._conn:
                self._maybe_gc(inode)

    def read(self, fd, size, offset):
        entry = self._fd_table.get(fd)
        access_mode = entry.flags & 0o3
        if access_mode == os.O_WRONLY:
            raise PermissionDenied("fd opened O_WRONLY")
        node = nodes.get(self._conn, entry.inode)
        return blobs.read_range(
            self._conn, entry.inode, offset, size,
            file_size=node.size, chunk_size=self._chunk_size_val,
        )

    def write(self, fd, data, offset):
        self._require_writable()
        entry = self._fd_table.get(fd)
        access_mode = entry.flags & 0o3
        if access_mode == os.O_RDONLY:
            raise PermissionDenied("fd opened O_RDONLY")
        node = nodes.get(self._conn, entry.inode)
        now = time.time_ns()
        with self._conn:
            new_size = blobs.write_range(
                self._conn, entry.inode, data, offset,
                file_size=node.size, chunk_size=self._chunk_size_val,
            )
            nodes.update_size(self._conn, entry.inode, new_size, now, now)
        path = self._path_of_inode(entry.inode)
        if path is not None:
            self._emit(Event(
                kind="modify", path=path, src_path=None, dst_path=None,
                node_kind="file", inode=entry.inode, timestamp_ns=now,
            ))
        return len(data)

    def truncate_fd(self, fd, size):
        self._require_writable()
        entry = self._fd_table.get(fd)
        node = nodes.get(self._conn, entry.inode)
        now = time.time_ns()
        with self._conn:
            blobs.truncate_to(
                self._conn, entry.inode, size,
                old_size=node.size, chunk_size=self._chunk_size_val,
            )
            nodes.update_size(self._conn, entry.inode, size, now, now)
        path = self._path_of_inode(entry.inode)
        if path is not None:
            self._emit(Event(
                kind="modify", path=path, src_path=None, dst_path=None,
                node_kind="file", inode=entry.inode, timestamp_ns=now,
            ))

    def truncate(self, path, size):
        self._require_writable()
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        if node.kind == "dir":
            raise IsADirectory(f"is a directory: {path!r}")
        now = time.time_ns()
        with self._conn:
            blobs.truncate_to(
                self._conn, inode, size,
                old_size=node.size, chunk_size=self._chunk_size_val,
            )
            nodes.update_size(self._conn, inode, size, now, now)
        self._emit(Event(
            kind="modify", path=path, src_path=None, dst_path=None,
            node_kind="file", inode=inode, timestamp_ns=now,
        ))

    def fsync(self, fd, datasync=False):
        self._conn.commit()

    def unlink(self, path):
        self._require_writable()
        parent_inode, name = self._resolve_parent(path)
        entry = entries.get(self._conn, parent_inode, name)
        node = nodes.get(self._conn, entry.inode)
        if node.kind == "dir":
            raise IsADirectory(f"{path!r} is a directory; use rmdir")
        removed_inode = entry.inode
        removed_kind = node.kind
        now = time.time_ns()
        with self._conn:
            entries.delete(self._conn, parent_inode, name)
            nodes.change_nlink(self._conn, entry.inode, -1, now)
            nodes.update_times(self._conn, parent_inode,
                               mtime_ns=now, ctime_ns=now)
            self._maybe_gc(entry.inode)
        self._path_cache.pop(removed_inode, None)
        self._emit(Event(
            kind="remove", path=path, src_path=None, dst_path=None,
            node_kind=removed_kind, inode=removed_inode, timestamp_ns=now,
        ))

    # --- links ---

    def symlink(self, target, linkpath):
        self._require_writable()
        if not isinstance(target, bytes):
            raise InvalidArgument("symlink target must be bytes")
        parent_inode, name = self._resolve_parent(linkpath)
        try:
            entries.get(self._conn, parent_inode, name)
            raise AlreadyExists(f"path exists: {linkpath!r}")
        except NotFound:
            pass
        now = time.time_ns()
        with self._conn:
            inode = nodes.insert(
                self._conn, "symlink", 0o777,
                self._uid, self._gid, now,
            )
            entries.insert(self._conn, parent_inode, name, inode)
            symlinks_mod.insert(self._conn, inode, target)
            nodes.update_times(self._conn, parent_inode,
                               mtime_ns=now, ctime_ns=now)
        self._emit(Event(
            kind="create", path=linkpath, src_path=None, dst_path=None,
            node_kind="symlink", inode=inode, timestamp_ns=now,
        ))

    def readlink(self, path):
        inode = self._resolve_path(path, follow_final_symlink=False)
        node = nodes.get(self._conn, inode)
        if node.kind != "symlink":
            raise NotFound(f"{path!r} is not a symlink")
        return symlinks_mod.get(self._conn, inode)

    def link(self, src, dst):
        """plan.v3: hard link. Adds a second entry pointing at src's inode."""
        self._require_writable()
        src_inode = self._resolve_path(src)
        src_node = nodes.get(self._conn, src_inode)
        if src_node.kind == "dir":
            raise PermissionDenied("cannot hard-link to a directory")
        dst_parent, dst_name = self._resolve_parent(dst)
        try:
            entries.get(self._conn, dst_parent, dst_name)
            raise AlreadyExists(f"path exists: {dst!r}")
        except NotFound:
            pass
        now = time.time_ns()
        with self._conn:
            entries.insert(self._conn, dst_parent, dst_name, src_inode)
            nodes.change_nlink(self._conn, src_inode, +1, now)
            nodes.update_times(self._conn, dst_parent,
                               mtime_ns=now, ctime_ns=now)
        self._emit(Event(
            kind="create", path=dst, src_path=None, dst_path=None,
            node_kind=src_node.kind, inode=src_inode, timestamp_ns=now,
        ))

    # --- xattrs ---

    def getxattr(self, path, name):
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, Access.R)
        return xattrs_mod.get(self._conn, inode, name)

    def setxattr(self, path, name, value, *, flags=0):
        self._require_writable()
        xattrs_mod.validate_name(name, self._uid)
        xattrs_mod.validate_value(value)
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, Access.W)
        with self._conn:
            xattrs_mod.set(self._conn, inode, name, value, flags)
        self._emit(Event(
            kind="metadata", path=path, src_path=None, dst_path=None,
            node_kind=node.kind, inode=inode,
            timestamp_ns=time.time_ns(),
        ))

    def listxattr(self, path):
        inode = self._resolve_path(path)
        return xattrs_mod.list_names(self._conn, inode)

    def removexattr(self, path, name):
        self._require_writable()
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, Access.W)
        with self._conn:
            xattrs_mod.remove(self._conn, inode, name)
        self._emit(Event(
            kind="metadata", path=path, src_path=None, dst_path=None,
            node_kind=node.kind, inode=inode,
            timestamp_ns=time.time_ns(),
        ))

    # --- rename ---

    def rename(self, src, dst, *, noreplace=False, exchange=False):
        self._require_writable()
        if noreplace and exchange:
            raise InvalidArgument("noreplace and exchange are mutually exclusive")

        src_parent, src_name = self._resolve_parent(src)
        dst_parent, dst_name = self._resolve_parent(dst)
        src_entry = entries.get(self._conn, src_parent, src_name)
        src_node = nodes.get(self._conn, src_entry.inode)

        if src_parent == dst_parent and src_name == dst_name:
            return

        # Into-own-subtree check (dirs only — files/symlinks can't be ancestors).
        if src_node.kind == "dir":
            dst_ancestry = nodes.ancestry(self._conn, dst_parent)
            if src_entry.inode in dst_ancestry or src_entry.inode == dst_parent:
                raise InvalidArgument("rename into own subtree")

        try:
            dst_entry = entries.get(self._conn, dst_parent, dst_name)
        except NotFound:
            dst_entry = None

        now = time.time_ns()

        with self._conn:
            if exchange:
                if dst_entry is None:
                    raise NotFound(f"exchange target missing: {dst!r}")
                entries.rename(self._conn, src_parent, src_name,
                               dst_parent, "__swap_tmp__")
                entries.rename(self._conn, dst_parent, dst_name,
                               src_parent, src_name)
                entries.rename(self._conn, dst_parent, "__swap_tmp__",
                               dst_parent, dst_name)
            else:
                if dst_entry is not None:
                    if noreplace:
                        raise AlreadyExists(f"target exists: {dst!r}")
                    dst_node = nodes.get(self._conn, dst_entry.inode)
                    if dst_node.kind == "dir":
                        if entries.count(self._conn, dst_entry.inode) > 0:
                            raise DirectoryNotEmpty(
                                f"target dir not empty: {dst!r}"
                            )
                        entries.delete(self._conn, dst_parent, dst_name)
                        nodes.delete(self._conn, dst_entry.inode)
                    else:
                        entries.delete(self._conn, dst_parent, dst_name)
                        nodes.change_nlink(self._conn, dst_entry.inode, -1, now)
                        self._maybe_gc(dst_entry.inode)
                entries.rename(self._conn, src_parent, src_name,
                               dst_parent, dst_name)

            nodes.update_times(self._conn, src_parent,
                               mtime_ns=now, ctime_ns=now)
            if dst_parent != src_parent:
                nodes.update_times(self._conn, dst_parent,
                                   mtime_ns=now, ctime_ns=now)

        # plan.v5: rename invalidates path cache for the renamed inode
        # (and, for dir renames, potentially its whole subtree — simplest
        # and safest is to clear the entire cache).
        if src_node.kind == "dir":
            self._path_cache.clear()
        else:
            self._path_cache.pop(src_entry.inode, None)
            if exchange and dst_entry:
                self._path_cache.pop(dst_entry.inode, None)

        # plan.v4: emit move event(s). Exchange emits two; plain emits one.
        if exchange:
            self._emit(Event(
                kind="move", path=dst, src_path=src, dst_path=dst,
                node_kind=src_node.kind, inode=src_entry.inode,
                timestamp_ns=now,
            ))
            dst_node_kind = nodes.get(self._conn, dst_entry.inode).kind
            self._emit(Event(
                kind="move", path=src, src_path=dst, dst_path=src,
                node_kind=dst_node_kind, inode=dst_entry.inode,
                timestamp_ns=now,
            ))
        else:
            self._emit(Event(
                kind="move", path=dst, src_path=src, dst_path=dst,
                node_kind=src_node.kind, inode=src_entry.inode,
                timestamp_ns=now,
            ))

    # --- fsck ---

    def fsck(self):
        from sqlite_fs.fsck import run_fsck
        return run_fsck(self._conn)

    # --- locks (thin delegation) ---

    def posix_lock(self, fd, op, start, length, *, wait=False):
        entry = self._fd_table.get(fd)
        pid = os.getpid()
        self._lock_mgr.posix_lock(entry.inode, fd, pid, op, start, length, wait=wait)

    def ofd_lock(self, fd, op, start, length, *, wait=False):
        entry = self._fd_table.get(fd)
        self._lock_mgr.ofd_lock(entry.inode, fd, op, start, length, wait=wait)

    def flock(self, fd, op, *, wait=False):
        entry = self._fd_table.get(fd)
        self._lock_mgr.flock(entry.inode, fd, op, wait=wait)

    def posix_getlk(self, fd, start, length):
        entry = self._fd_table.get(fd)
        pid = os.getpid()
        return self._lock_mgr.posix_getlk(entry.inode, fd, pid, start, length)

    def ofd_getlk(self, fd, start, length):
        entry = self._fd_table.get(fd)
        return self._lock_mgr.ofd_getlk(entry.inode, fd, start, length)

    # --- test hooks ---

    def _count_chunks(self, inode):
        return blobs.count_chunks(self._conn, inode)

    def _total_blob_bytes(self, inode):
        return blobs.total_bytes(self._conn, inode)

    def _chunk_size(self):
        return self._chunk_size_val

    def _sqlite_pragma(self, name):
        return self._conn.execute(f"PRAGMA {name}").fetchone()[0]

    def _row_exists(self, key, inode):
        if key == "nodes":
            try:
                nodes.get(self._conn, inode)
                return True
            except NotFound:
                return False
        if key == "blobs":
            return blobs.count_chunks(self._conn, inode) > 0
        if key == "xattrs":
            return xattrs_mod.has_any(self._conn, inode)
        if key == "symlinks":
            return symlinks_mod.exists(self._conn, inode)
        raise ValueError(f"unknown key: {key}")
