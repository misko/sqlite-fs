import os
import sqlite3
import time
from contextlib import contextmanager

from sqlite_fs import blobs, nodes
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


class Filesystem:
    def __init__(self, conn, *, readonly, uid, gid):
        self._conn = conn
        self._readonly = readonly
        self._uid = uid
        self._gid = gid
        apply_pragmas(conn)
        self._chunk_size = load_chunk_size(conn)
        self._fd_table = FdTable()
        self._lock_mgr = LockManager()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self._conn.commit()
            if not self._readonly:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        finally:
            self._conn.close()

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
            child = nodes.get_child(self._conn, cur, name)
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
        now = time.time_ns()
        try:
            with self._conn:
                nodes.insert(self._conn, parent_inode, name,
                             "dir", mode, self._uid, self._gid, now)
                nodes.change_nlink(self._conn, parent_inode, +1, now)
                nodes.update_times(self._conn, parent_inode,
                                   mtime_ns=now, ctime_ns=now)
        except sqlite3.IntegrityError:
            raise AlreadyExists(f"path exists: {path!r}")

    def rmdir(self, path):
        self._require_writable()
        if path == "/":
            raise PermissionDenied("cannot remove root")
        parent_inode, name = self._resolve_parent(path)
        node = nodes.get_child(self._conn, parent_inode, name)
        if node.kind != "dir":
            raise NotADirectory(f"{path!r} is not a directory")
        if nodes.count_children(self._conn, node.inode) > 0:
            raise DirectoryNotEmpty(f"directory not empty: {path!r}")
        now = time.time_ns()
        with self._conn:
            nodes.delete(self._conn, node.inode)
            nodes.change_nlink(self._conn, parent_inode, -1, now)
            nodes.update_times(self._conn, parent_inode,
                               mtime_ns=now, ctime_ns=now)

    def readdir(self, path):
        inode = self._resolve_path(path)
        node = nodes.get(self._conn, inode)
        if node.kind != "dir":
            raise NotADirectory(f"{path!r} is not a directory")
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, Access.R)
        rows = nodes.list_children(self._conn, inode)
        return [DirEntry(name=r.name, kind=r.kind, inode=r.inode) for r in rows]

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

    # --- file ---

    def create(self, path, mode=0o644, flags=0):
        # Spec finding: engspec fs-10 said O_WRONLY (POSIX creat(2) semantic),
        # but test_blobs uses create()'s fd for both write and read. O_RDWR
        # matches what callers actually want from an ergonomic Python API.
        # Record in plan.v3: create() returns an RDWR fd.
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
            exists = True
        except NotFound:
            exists = False
            inode = None

        if exists:
            if creat and excl:
                raise AlreadyExists(f"path exists: {path!r}")
            if nofollow:
                # If final was a symlink, _resolve_path with follow_final=False
                # returns the symlink inode itself. Reject.
                node_check = nodes.get(self._conn, inode)
                if node_check.kind == "symlink":
                    raise SymlinkLoop(f"O_NOFOLLOW refused: {path!r} is a symlink")
        else:
            if not creat:
                raise NotFound(f"no such file: {path!r}")
            self._require_writable()
            parent_inode, name = self._resolve_parent(path)
            now = time.time_ns()
            try:
                with self._conn:
                    inode = nodes.insert(
                        self._conn, parent_inode, name,
                        "file", mode & 0o7777, self._uid, self._gid, now,
                    )
                    nodes.update_times(self._conn, parent_inode,
                                       mtime_ns=now, ctime_ns=now)
            except sqlite3.IntegrityError:
                raise AlreadyExists(f"path exists: {path!r}")

        # Access check based on requested mode.
        node = nodes.get(self._conn, inode)
        access_bits = Access(0)
        access_mode = flags & 0o3   # O_RDONLY=0, O_WRONLY=1, O_RDWR=2
        if access_mode in (os.O_RDONLY, os.O_RDWR):
            access_bits |= Access.R
        if access_mode in (os.O_WRONLY, os.O_RDWR):
            access_bits |= Access.W
        if access_bits == Access(0):
            access_bits = Access.R  # O_RDONLY default
        require_access(node.mode, node.uid, node.gid,
                       self._uid, self._gid, access_bits)

        # Truncate if requested.
        if trunc and exists and node.kind == "file":
            self._require_writable()
            now = time.time_ns()
            with self._conn:
                blobs.truncate_to(self._conn, inode,
                                  0, old_size=node.size,
                                  chunk_size=self._chunk_size)
                nodes.update_size(self._conn, inode, 0, now, now)

        return self._fd_table.open(inode, flags, self._uid, self._gid)

    def close_fd(self, fd):
        entry = self._fd_table.get(fd)
        inode = entry.inode
        pid = os.getpid()
        self._fd_table.close(fd)
        self._lock_mgr.on_fd_close(inode, fd, pid)
        # Trigger GC if this fd was the last reference to an unlinked inode.
        if not self._readonly:
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
            file_size=node.size, chunk_size=self._chunk_size,
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
                file_size=node.size, chunk_size=self._chunk_size,
            )
            nodes.update_size(self._conn, entry.inode, new_size, now, now)
        return len(data)

    def truncate_fd(self, fd, size):
        self._require_writable()
        entry = self._fd_table.get(fd)
        node = nodes.get(self._conn, entry.inode)
        now = time.time_ns()
        with self._conn:
            blobs.truncate_to(
                self._conn, entry.inode, size,
                old_size=node.size, chunk_size=self._chunk_size,
            )
            nodes.update_size(self._conn, entry.inode, size, now, now)

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
                old_size=node.size, chunk_size=self._chunk_size,
            )
            nodes.update_size(self._conn, inode, size, now, now)

    def fsync(self, fd, datasync=False):
        self._conn.commit()

    def unlink(self, path):
        self._require_writable()
        parent_inode, name = self._resolve_parent(path)
        node = nodes.get_child(self._conn, parent_inode, name)
        if node.kind == "dir":
            raise IsADirectory(f"{path!r} is a directory; use rmdir")
        now = time.time_ns()
        with self._conn:
            # Detach from parent by deleting the directory entry. For v1
            # (no hard links — see fs.link stub), unlinking means removing
            # the node. Any held fds remain usable via _maybe_gc.
            nodes.change_nlink(self._conn, node.inode, -1, now)
            nodes.update_times(self._conn, parent_inode,
                               mtime_ns=now, ctime_ns=now)
            self._maybe_gc(node.inode)

    # --- links ---

    def symlink(self, target, linkpath):
        self._require_writable()
        if not isinstance(target, bytes):
            raise InvalidArgument("symlink target must be bytes")
        parent_inode, name = self._resolve_parent(linkpath)
        now = time.time_ns()
        try:
            with self._conn:
                inode = nodes.insert(
                    self._conn, parent_inode, name,
                    "symlink", 0o777, self._uid, self._gid, now,
                )
                symlinks_mod.insert(self._conn, inode, target)
                nodes.update_times(self._conn, parent_inode,
                                   mtime_ns=now, ctime_ns=now)
        except sqlite3.IntegrityError:
            raise AlreadyExists(f"path exists: {linkpath!r}")

    def readlink(self, path):
        inode = self._resolve_path(path, follow_final_symlink=False)
        node = nodes.get(self._conn, inode)
        if node.kind != "symlink":
            raise NotFound(f"{path!r} is not a symlink")
        return symlinks_mod.get(self._conn, inode)

    def link(self, src, dst):
        # v1 limitation: hard links require a schema change (target_inode
        # pointer column) that we've deferred. Raise a clear error.
        raise NotImplementedError(
            "hard links are not yet implemented in this v1 smoke-test slice; "
            "see package/specs/src/fs.py.engspec § fs-11 for the plan.v3 "
            "schema-change proposal."
        )

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

    # --- rename ---

    def rename(self, src, dst, *, noreplace=False, exchange=False):
        self._require_writable()
        if noreplace and exchange:
            raise InvalidArgument("noreplace and exchange are mutually exclusive")

        src_parent, src_name = self._resolve_parent(src)
        dst_parent, dst_name = self._resolve_parent(dst)
        src_node = nodes.get_child(self._conn, src_parent, src_name)

        if src_parent == dst_parent and src_name == dst_name:
            return

        dst_ancestry = nodes.ancestry(self._conn, dst_parent)
        if src_node.inode in dst_ancestry or src_node.inode == dst_parent:
            raise InvalidArgument("rename into own subtree")

        try:
            dst_node = nodes.get_child(self._conn, dst_parent, dst_name)
        except NotFound:
            dst_node = None

        now = time.time_ns()

        with self._conn:
            if exchange:
                if dst_node is None:
                    raise NotFound(f"exchange target missing: {dst!r}")
                nodes.rename_entry(self._conn, src_node.inode,
                                   dst_parent, "__swap_tmp__", now)
                nodes.rename_entry(self._conn, dst_node.inode,
                                   src_parent, src_name, now)
                nodes.rename_entry(self._conn, src_node.inode,
                                   dst_parent, dst_name, now)
            else:
                if dst_node is not None:
                    if noreplace:
                        raise AlreadyExists(f"target exists: {dst!r}")
                    if dst_node.kind == "dir":
                        if nodes.count_children(self._conn, dst_node.inode) > 0:
                            raise DirectoryNotEmpty(f"target dir not empty: {dst!r}")
                        nodes.delete(self._conn, dst_node.inode)
                    else:
                        nodes.change_nlink(self._conn, dst_node.inode, -1, now)
                        self._maybe_gc(dst_node.inode)
                nodes.rename_entry(self._conn, src_node.inode,
                                   dst_parent, dst_name, now)

            nodes.update_times(self._conn, src_parent,
                               mtime_ns=now, ctime_ns=now)
            if dst_parent != src_parent:
                nodes.update_times(self._conn, dst_parent,
                                   mtime_ns=now, ctime_ns=now)

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
