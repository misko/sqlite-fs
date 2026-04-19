"""sqlite-fs — a durable, performant FUSE filesystem backed by SQLite."""

from sqlite_fs.errors import (
    AlreadyExists,
    BadFileDescriptor,
    DirectoryNotEmpty,
    FilesystemError,
    InvalidArgument,
    InvalidXattr,
    IsADirectory,
    LockConflict,
    NameTooLong,
    NotADirectory,
    NotFound,
    PathSyntaxError,
    PermissionDenied,
    ReadOnlyFilesystem,
    SymlinkLoop,
)
from sqlite_fs.fs import Filesystem
from sqlite_fs.mkfs import mkfs, open_fs
from sqlite_fs.types import (
    Access,
    DirEntry,
    FlockOp,
    FsckIssue,
    FsckReport,
    LockOp,
    LockQuery,
    Stat,
)
from sqlite_fs.watch import Event, Watcher

__version__ = "0.1.0"

__all__ = [
    "mkfs",
    "open_fs",
    "Filesystem",
    "Access",
    "DirEntry",
    "Event",
    "FlockOp",
    "FsckIssue",
    "FsckReport",
    "LockOp",
    "LockQuery",
    "Stat",
    "Watcher",
    "AlreadyExists",
    "BadFileDescriptor",
    "DirectoryNotEmpty",
    "FilesystemError",
    "InvalidArgument",
    "InvalidXattr",
    "IsADirectory",
    "LockConflict",
    "NameTooLong",
    "NotADirectory",
    "NotFound",
    "PathSyntaxError",
    "PermissionDenied",
    "ReadOnlyFilesystem",
    "SymlinkLoop",
]
