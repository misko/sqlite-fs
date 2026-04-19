"""pyfuse3 adapter over sqlite_fs.Filesystem.

Translates kernel-VFS callbacks to library calls. Adds bytes↔str handling
for names, inode↔path mapping, `FilesystemError.errno` → `FUSEError(errno)`
translation, and pyfuse3 EntryAttributes construction.
"""
import errno
import os
import stat as stat_mod
import subprocess

import pyfuse3
import trio

from sqlite_fs import Filesystem, blobs, entries, nodes
from sqlite_fs import symlinks as symlinks_mod
from sqlite_fs import xattrs as xattrs_mod
from sqlite_fs.errors import (
    AlreadyExists,
    BadFileDescriptor,
    DirectoryNotEmpty,
    FilesystemError,
    InvalidArgument,
    InvalidXattr,
    IsADirectory,
    NotADirectory,
    NotFound,
    PermissionDenied,
    ReadOnlyFilesystem,
    SymlinkLoop,
)
from sqlite_fs.perms import Access, check_access


_KIND_TO_MODE_BITS = {
    "file": stat_mod.S_IFREG,
    "dir": stat_mod.S_IFDIR,
    "symlink": stat_mod.S_IFLNK,
}


def _to_entry_attributes(node_inode, node):
    ea = pyfuse3.EntryAttributes()
    ea.st_ino = node_inode
    ea.st_nlink = node.nlink
    ea.st_size = node.size
    ea.st_mode = (node.mode & 0o7777) | _KIND_TO_MODE_BITS[node.kind]
    ea.st_uid = node.uid
    ea.st_gid = node.gid
    ea.st_atime_ns = node.atime_ns
    ea.st_mtime_ns = node.mtime_ns
    ea.st_ctime_ns = node.ctime_ns
    ea.generation = 0
    ea.entry_timeout = 0
    ea.attr_timeout = 0
    ea.st_blksize = 4096
    ea.st_blocks = (node.size + 511) // 512
    return ea


def _fuse_error_from(err):
    """Translate a FilesystemError to a pyfuse3.FUSEError."""
    return pyfuse3.FUSEError(getattr(err, "errno", errno.EIO))


class Adapter(pyfuse3.Operations):
    supports_dot_lookup = False
    enable_writeback_cache = False
    enable_acl = False

    def __init__(self, fs: Filesystem):
        super().__init__()
        self._fs = fs
        # Map libfuse fh → library fd. We use the library fd directly as fh.
        # Both are positive integers, both monotonic per-Filesystem.

    # --- helpers ---

    def _path_from_inode(self, inode):
        """Build an absolute path from an inode by walking up via entries."""
        if inode == 1:
            return "/"
        parts = []
        cur = inode
        while cur != 1:
            row = self._fs._conn.execute(
                "SELECT parent, name FROM entries WHERE inode = ? LIMIT 1",
                (cur,),
            ).fetchone()
            if row is None:
                raise pyfuse3.FUSEError(errno.ENOENT)
            parts.append(row[1])
            cur = row[0]
        return "/" + "/".join(reversed(parts))

    def _child_path(self, parent_inode, name):
        parent_path = self._path_from_inode(parent_inode)
        name_str = os.fsdecode(name)
        if parent_path == "/":
            return "/" + name_str, name_str
        return parent_path + "/" + name_str, name_str

    def _with_ctx(self, ctx):
        if ctx is None:
            return self._fs.as_user(self._fs._uid, self._fs._gid)
        return self._fs.as_user(ctx.uid, ctx.gid)

    # --- metadata ---

    async def getattr(self, inode, ctx=None):
        try:
            with self._with_ctx(ctx):
                node = nodes.get(self._fs._conn, inode)
                return _to_entry_attributes(inode, node)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def setattr(self, inode, attr, fields, fh, ctx):
        try:
            with self._with_ctx(ctx):
                # Chain of targeted updates.
                if fields.update_mode:
                    path = self._path_from_inode(inode)
                    self._fs.chmod(path, attr.st_mode & 0o7777,
                                   follow_symlinks=False)
                if fields.update_uid or fields.update_gid:
                    path = self._path_from_inode(inode)
                    node = nodes.get(self._fs._conn, inode)
                    new_uid = attr.st_uid if fields.update_uid else node.uid
                    new_gid = attr.st_gid if fields.update_gid else node.gid
                    self._fs.chown(path, new_uid, new_gid,
                                   follow_symlinks=False)
                if fields.update_atime or fields.update_mtime:
                    path = self._path_from_inode(inode)
                    node = nodes.get(self._fs._conn, inode)
                    new_atime = (attr.st_atime_ns
                                 if fields.update_atime else node.atime_ns)
                    new_mtime = (attr.st_mtime_ns
                                 if fields.update_mtime else node.mtime_ns)
                    self._fs.utimes(path, new_atime, new_mtime,
                                    follow_symlinks=False)
                if fields.update_size:
                    path = self._path_from_inode(inode)
                    self._fs.truncate(path, attr.st_size)
                node = nodes.get(self._fs._conn, inode)
                return _to_entry_attributes(inode, node)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def lookup(self, parent_inode, name, ctx=None):
        try:
            with self._with_ctx(ctx):
                name_str = os.fsdecode(name)
                entry = entries.get(self._fs._conn, parent_inode, name_str)
                node = nodes.get(self._fs._conn, entry.inode)
                return _to_entry_attributes(entry.inode, node)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def forget(self, inode_list):
        # No ref counting in v1.
        return

    # --- directory ops ---

    async def mkdir(self, parent_inode, name, mode, ctx):
        try:
            with self._with_ctx(ctx):
                child_path, _ = self._child_path(parent_inode, name)
                self._fs.mkdir(child_path, mode=mode & 0o7777)
                entry = entries.get(self._fs._conn, parent_inode,
                                    os.fsdecode(name))
                node = nodes.get(self._fs._conn, entry.inode)
                return _to_entry_attributes(entry.inode, node)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def rmdir(self, parent_inode, name, ctx):
        try:
            with self._with_ctx(ctx):
                child_path, _ = self._child_path(parent_inode, name)
                self._fs.rmdir(child_path)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def unlink(self, parent_inode, name, ctx):
        try:
            with self._with_ctx(ctx):
                child_path, _ = self._child_path(parent_inode, name)
                self._fs.unlink(child_path)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def opendir(self, inode, ctx):
        return inode

    async def readdir(self, inode, off, token):
        try:
            rows = entries.list_(self._fs._conn, inode)
            # off is the offset to resume from.
            for i, entry in enumerate(rows[off:], start=off + 1):
                node = nodes.get(self._fs._conn, entry.inode)
                attr = _to_entry_attributes(entry.inode, node)
                name_bytes = os.fsencode(entry.name)
                if not pyfuse3.readdir_reply(token, name_bytes, attr, i):
                    break
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def releasedir(self, inode):
        return

    # --- file ops ---

    async def open(self, inode, flags, ctx):
        try:
            with self._with_ctx(ctx):
                # The VFS already resolved the path → inode. Open directly on
                # the inode, bypassing path resolution.
                node = nodes.get(self._fs._conn, inode)
                access_bits = Access(0)
                access_mode = flags & 0o3
                if access_mode in (os.O_RDONLY, os.O_RDWR):
                    access_bits |= Access.R
                if access_mode in (os.O_WRONLY, os.O_RDWR):
                    access_bits |= Access.W
                if access_bits == Access(0):
                    access_bits = Access.R
                from sqlite_fs.perms import require_access
                require_access(node.mode, node.uid, node.gid,
                               self._fs._uid, self._fs._gid, access_bits)
                if flags & os.O_TRUNC and node.kind == "file":
                    with self._fs._conn:
                        blobs.truncate_to(
                            self._fs._conn, inode, 0,
                            old_size=node.size, chunk_size=self._fs._chunk_size_val,
                        )
                        import time as _time
                        now = _time.time_ns()
                        nodes.update_size(self._fs._conn, inode, 0, now, now)
                fd = self._fs._fd_table.open(inode, flags, self._fs._uid, self._fs._gid)
                fi = pyfuse3.FileInfo(fh=fd)
                return fi
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def create(self, parent_inode, name, mode, flags, ctx):
        try:
            with self._with_ctx(ctx):
                child_path, _ = self._child_path(parent_inode, name)
                fd = self._fs.open(
                    child_path,
                    flags=flags | os.O_CREAT,
                    mode=mode & 0o7777,
                )
                entry = entries.get(self._fs._conn, parent_inode,
                                    os.fsdecode(name))
                node = nodes.get(self._fs._conn, entry.inode)
                fi = pyfuse3.FileInfo(fh=fd)
                return (fi, _to_entry_attributes(entry.inode, node))
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def read(self, fh, off, size):
        try:
            # Temporarily relax access check by reading blobs directly.
            # The fd was opened with caller's uid/gid; the library check ran
            # at open time. Read now just reads bytes.
            entry = self._fs._fd_table.get(fh)
            node = nodes.get(self._fs._conn, entry.inode)
            return blobs.read_range(
                self._fs._conn, entry.inode, off, size,
                file_size=node.size, chunk_size=self._fs._chunk_size_val,
            )
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def write(self, fh, off, buf):
        try:
            entry = self._fs._fd_table.get(fh)
            node = nodes.get(self._fs._conn, entry.inode)
            import time as _time
            now = _time.time_ns()
            with self._fs._conn:
                new_size = blobs.write_range(
                    self._fs._conn, entry.inode, buf, off,
                    file_size=node.size, chunk_size=self._fs._chunk_size_val,
                )
                nodes.update_size(self._fs._conn, entry.inode, new_size, now, now)
            return len(buf)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def release(self, fh):
        try:
            self._fs.close_fd(fh)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def flush(self, fh):
        try:
            self._fs._conn.commit()
        except Exception:
            pass

    async def fsync(self, fh, datasync):
        try:
            self._fs._conn.commit()
        except Exception:
            pass

    async def fsyncdir(self, inode, datasync):
        try:
            self._fs._conn.commit()
        except Exception:
            pass

    # --- links ---

    async def readlink(self, inode, ctx=None):
        try:
            with self._with_ctx(ctx or pyfuse3.RequestContext()):
                return symlinks_mod.get(self._fs._conn, inode)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def symlink(self, parent_inode, name, target, ctx):
        try:
            with self._with_ctx(ctx):
                child_path, _ = self._child_path(parent_inode, name)
                # target is bytes from kernel; pass through as-is.
                self._fs.symlink(bytes(target), child_path)
                entry = entries.get(self._fs._conn, parent_inode,
                                    os.fsdecode(name))
                node = nodes.get(self._fs._conn, entry.inode)
                return _to_entry_attributes(entry.inode, node)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def link(self, inode, new_parent_inode, new_name, ctx):
        try:
            with self._with_ctx(ctx):
                # src is identified by inode; dst by (new_parent_inode, new_name).
                src_path = self._path_from_inode(inode)
                dst_path, _ = self._child_path(new_parent_inode, new_name)
                self._fs.link(src_path, dst_path)
                node = nodes.get(self._fs._conn, inode)
                return _to_entry_attributes(inode, node)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def rename(self, parent_old, name_old, parent_new, name_new,
                     flags, ctx):
        try:
            with self._with_ctx(ctx):
                src_path, _ = self._child_path(parent_old, name_old)
                dst_path, _ = self._child_path(parent_new, name_new)
                noreplace = bool(flags & 1)   # RENAME_NOREPLACE = 1
                exchange = bool(flags & 2)    # RENAME_EXCHANGE = 2
                self._fs.rename(src_path, dst_path,
                                noreplace=noreplace, exchange=exchange)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    # --- xattrs ---

    async def setxattr(self, inode, name, value, ctx):
        try:
            with self._with_ctx(ctx):
                path = self._path_from_inode(inode)
                self._fs.setxattr(path, os.fsdecode(name), bytes(value))
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def getxattr(self, inode, name, ctx):
        try:
            with self._with_ctx(ctx):
                path = self._path_from_inode(inode)
                return self._fs.getxattr(path, os.fsdecode(name))
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def listxattr(self, inode, ctx):
        try:
            with self._with_ctx(ctx):
                path = self._path_from_inode(inode)
                names = self._fs.listxattr(path)
                return [os.fsencode(n) for n in names]
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def removexattr(self, inode, name, ctx):
        try:
            with self._with_ctx(ctx):
                path = self._path_from_inode(inode)
                self._fs.removexattr(path, os.fsdecode(name))
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    # --- access/statfs ---

    async def access(self, inode, mode, ctx):
        try:
            with self._with_ctx(ctx):
                node = nodes.get(self._fs._conn, inode)
                access_flag = Access(0)
                if mode & os.R_OK: access_flag |= Access.R
                if mode & os.W_OK: access_flag |= Access.W
                if mode & os.X_OK: access_flag |= Access.X
                if access_flag == Access(0):
                    return  # F_OK: just check existence; we already did.
                if not check_access(node.mode, node.uid, node.gid,
                                    self._fs._uid, self._fs._gid, access_flag):
                    raise pyfuse3.FUSEError(errno.EACCES)
        except FilesystemError as e:
            raise _fuse_error_from(e)
        except Exception:
            import traceback as _tb, sys as _sys
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            raise pyfuse3.FUSEError(errno.EIO)

    async def statfs(self, ctx):
        st = pyfuse3.StatvfsData()
        st.f_bsize = 4096
        st.f_frsize = 4096
        st.f_blocks = 1 << 30   # nominal
        st.f_bfree = 1 << 30
        st.f_bavail = 1 << 30
        st.f_files = 1 << 30
        st.f_ffree = 1 << 30
        st.f_favail = 1 << 30
        st.f_namemax = 255
        return st


def mount(db_path, mountpoint, *, foreground=False, readonly=False,
          subdir=None):
    from sqlite_fs import open_fs

    fs = open_fs(db_path, readonly=readonly)
    adapter = Adapter(fs)

    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=sqlite-fs")
    if readonly:
        fuse_options.add("ro")

    pyfuse3.init(adapter, mountpoint, fuse_options)

    if foreground:
        try:
            trio.run(pyfuse3.main)
        finally:
            pyfuse3.close()
            fs.close()
    else:
        if os.fork() > 0:
            return
        try:
            trio.run(pyfuse3.main)
        finally:
            pyfuse3.close()
            fs.close()


def umount(mountpoint):
    subprocess.run(["fusermount3", "-u", mountpoint], check=True)
