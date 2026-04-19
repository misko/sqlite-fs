from dataclasses import dataclass

from sqlite_fs.errors import BadFileDescriptor


@dataclass
class FdEntry:
    fd: int
    inode: int
    flags: int
    uid: int
    gid: int
    offset: int = 0


class FdTable:
    def __init__(self):
        self._entries: dict[int, FdEntry] = {}
        self._next_fd: int = 1

    def open(self, inode, flags, uid, gid) -> int:
        fd = self._next_fd
        self._next_fd += 1
        self._entries[fd] = FdEntry(
            fd=fd, inode=inode, flags=flags, uid=uid, gid=gid,
        )
        return fd

    def get(self, fd) -> FdEntry:
        try:
            return self._entries[fd]
        except KeyError:
            raise BadFileDescriptor(f"unknown fd {fd}")

    def close(self, fd) -> int:
        try:
            entry = self._entries.pop(fd)
        except KeyError:
            raise BadFileDescriptor(f"unknown fd {fd}")
        return entry.inode

    def open_count(self, inode: int) -> int:
        return sum(1 for e in self._entries.values() if e.inode == inode)

    def fds_for_inode(self, inode: int) -> list:
        return [e.fd for e in self._entries.values() if e.inode == inode]
