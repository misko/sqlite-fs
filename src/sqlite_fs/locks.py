from dataclasses import dataclass
from typing import Literal, Optional

from sqlite_fs.errors import LockConflict
from sqlite_fs.types import FlockOp, LockOp, LockQuery


@dataclass
class _Record:
    kind: Literal["posix", "ofd", "flock"]
    type: Literal["shared", "exclusive"]
    owner: int
    start: int
    length: int  # 0 = to EOF


def _overlaps(a_start, a_length, b_start, b_length):
    """True if byte ranges [a_start, a_start+a_length) and [b_start, b_start+b_length)
    overlap. length=0 means 'to infinity'."""
    a_end = float("inf") if a_length == 0 else a_start + a_length
    b_end = float("inf") if b_length == 0 else b_start + b_length
    return not (a_end <= b_start or a_start >= b_end)


class LockManager:
    def __init__(self):
        self._by_inode = {}

    # --- POSIX ---

    def posix_lock(self, inode, fd_id, pid, op, start, length, *, wait=False):
        records = self._by_inode.setdefault(inode, [])

        if op == "unlock":
            self._release_posix_range(inode, pid, start, length)
            return

        # Scan for conflict from OTHER pids.
        for r in records:
            if r.kind != "posix":
                continue
            if r.owner == pid:
                continue
            if not _overlaps(r.start, r.length, start, length):
                continue
            # Conflict if either side is exclusive.
            if r.type == "exclusive" or op == "exclusive":
                raise LockConflict(
                    f"posix lock conflict: pid {r.owner} holds {r.type} "
                    f"on [{r.start}, {r.length})"
                )

        # Same-pid upgrade/downgrade: replace any existing same-pid record
        # covering the exact range.
        records[:] = [r for r in records if not (
            r.kind == "posix" and r.owner == pid
            and r.start == start and r.length == length
        )]
        records.append(_Record(
            kind="posix", type=op, owner=pid, start=start, length=length,
        ))

    def posix_getlk(self, inode, fd_id, pid, start, length):
        records = self._by_inode.get(inode, [])
        for r in records:
            if r.kind != "posix":
                continue
            if r.owner == pid:
                continue
            if not _overlaps(r.start, r.length, start, length):
                continue
            return LockQuery(
                type=r.type, pid=r.owner, start=r.start, length=r.length,
            )
        return None

    def _release_posix_range(self, inode, pid, start, length):
        """Remove every posix record with owner=pid that overlaps [start, start+length)."""
        records = self._by_inode.get(inode, [])
        remaining = []
        for r in records:
            if (r.kind == "posix" and r.owner == pid
                    and _overlaps(r.start, r.length, start, length)):
                continue
            remaining.append(r)
        if remaining:
            self._by_inode[inode] = remaining
        else:
            self._by_inode.pop(inode, None)

    # --- OFD ---

    def ofd_lock(self, inode, fd_id, op, start, length, *, wait=False):
        records = self._by_inode.setdefault(inode, [])

        if op == "unlock":
            records[:] = [r for r in records if not (
                r.kind == "ofd" and r.owner == fd_id
                and _overlaps(r.start, r.length, start, length)
            )]
            if not records:
                self._by_inode.pop(inode, None)
            return

        # Scan OFD records owned by OTHER fds.
        for r in records:
            if r.kind != "ofd":
                continue
            if r.owner == fd_id:
                continue
            if not _overlaps(r.start, r.length, start, length):
                continue
            if r.type == "exclusive" or op == "exclusive":
                raise LockConflict(
                    f"ofd lock conflict: fd {r.owner} holds {r.type}"
                )

        # Same-fd replacement.
        records[:] = [r for r in records if not (
            r.kind == "ofd" and r.owner == fd_id
            and r.start == start and r.length == length
        )]
        records.append(_Record(
            kind="ofd", type=op, owner=fd_id, start=start, length=length,
        ))

    def ofd_getlk(self, inode, fd_id, start, length):
        records = self._by_inode.get(inode, [])
        for r in records:
            if r.kind != "ofd":
                continue
            if r.owner == fd_id:
                continue
            if not _overlaps(r.start, r.length, start, length):
                continue
            return LockQuery(
                type=r.type, pid=r.owner, start=r.start, length=r.length,
            )
        return None

    # --- flock ---

    def flock(self, inode, fd_id, op, *, wait=False):
        records = self._by_inode.setdefault(inode, [])

        if op == "unlock":
            records[:] = [r for r in records if not (
                r.kind == "flock" and r.owner == fd_id
            )]
            if not records:
                self._by_inode.pop(inode, None)
            return

        # Scan flock records owned by OTHER fds.
        for r in records:
            if r.kind != "flock":
                continue
            if r.owner == fd_id:
                continue
            if r.type == "exclusive" or op == "exclusive":
                raise LockConflict(
                    f"flock lock conflict: fd {r.owner} holds {r.type}"
                )

        # Same-fd replacement.
        records[:] = [r for r in records if not (
            r.kind == "flock" and r.owner == fd_id
        )]
        records.append(_Record(
            kind="flock", type=op, owner=fd_id, start=0, length=0,
        ))

    # --- fd-close hook ---

    def on_fd_close(self, inode, fd_id, pid):
        records = self._by_inode.get(inode, [])
        remaining = []
        for r in records:
            if r.kind == "posix" and r.owner == pid:
                continue
            if r.kind == "ofd" and r.owner == fd_id:
                continue
            if r.kind == "flock" and r.owner == fd_id:
                continue
            remaining.append(r)
        if remaining:
            self._by_inode[inode] = remaining
        else:
            self._by_inode.pop(inode, None)
