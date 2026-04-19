"""Watch / event emission for sqlite-fs. plan.v4 feature.

In-process subscribers receive Event records for mutations happening
on the Filesystem they subscribed to. No kernel inotify integration
in this iteration — pyfuse3 notify is a v2.1 follow-up.
"""
from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional


EventKind = Literal["create", "remove", "modify", "move", "metadata"]
NodeKind = Literal["file", "dir", "symlink"]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    path: str
    src_path: Optional[str]     # for "move": old path; else None
    dst_path: Optional[str]     # for "move": new path; else None
    node_kind: NodeKind
    inode: int
    timestamp_ns: int


class Watcher:
    """Registered with a Filesystem via fs.watch(...). Iterate to receive
    events in FIFO order. Thread-unsafe per the v1 single-threaded daemon
    model."""

    def __init__(self, fs, path, *, recursive=False):
        self._fs = fs
        self._path = path
        self._recursive = recursive
        self._queue: deque = deque()
        self._closed = False
        # Register with the filesystem.
        fs._watchers.add(self)

    @property
    def path(self):
        return self._path

    @property
    def recursive(self):
        return self._recursive

    def _matches(self, event: Event) -> bool:
        # Compute the event's "parent path" — directory whose readdir
        # would change as a result of this event.
        candidates = [event.path]
        if event.kind == "move":
            # Both src and dst paths can match a watcher.
            if event.src_path:
                candidates.append(event.src_path)
        for candidate in candidates:
            parent = self._parent_of(candidate)
            if self._recursive:
                if candidate == self._path:
                    return True
                if candidate.startswith(self._path.rstrip("/") + "/"):
                    return True
            else:
                if parent == self._path:
                    return True
        return False

    @staticmethod
    def _parent_of(path: str) -> str:
        if path == "/" or "/" not in path[1:]:
            return "/"
        idx = path.rfind("/")
        if idx == 0:
            return "/"
        return path[:idx]

    def _enqueue(self, event: Event) -> None:
        if self._closed:
            return
        self._queue.append(event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Unregister from the filesystem.
        self._fs._watchers.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __iter__(self):
        return self

    def __next__(self) -> Event:
        if self._queue:
            return self._queue.popleft()
        if self._closed:
            raise StopIteration
        # Non-blocking semantics in v1: if no event is queued and the
        # watcher is still open, this raises StopIteration too. Callers
        # who want blocking should poll externally (v2.1 will add an
        # optional timeout).
        raise StopIteration
