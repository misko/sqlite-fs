from dataclasses import dataclass, field
from enum import Flag
from typing import Literal, Optional


LockOp = Literal["shared", "exclusive", "unlock"]
FlockOp = Literal["shared", "exclusive", "unlock"]

NodeKind = Literal["file", "dir", "symlink"]


class Access(Flag):
    R = 4
    W = 2
    X = 1


@dataclass(frozen=True)
class Stat:
    kind: NodeKind
    size: int
    mode: int
    uid: int
    gid: int
    atime_ns: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    inode: int


@dataclass(frozen=True)
class DirEntry:
    name: str
    kind: NodeKind
    inode: int


@dataclass(frozen=True)
class LockQuery:
    type: Literal["shared", "exclusive"]
    pid: int
    start: int
    length: int


@dataclass(frozen=True)
class FsckIssue:
    kind: Literal["orphan_blob", "orphan_xattr", "orphan_symlink",
                  "cycle", "nlink_mismatch", "dangling_parent"]
    inode: Optional[int]
    detail: str


@dataclass(frozen=True)
class FsckReport:
    integrity_check_result: Literal["ok", "corrupted"]
    issues: list
