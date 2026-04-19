import errno as _errno


class FilesystemError(Exception):
    """Library-wide base. No errno — never raised directly."""


class PathSyntaxError(FilesystemError, ValueError):
    errno = _errno.EINVAL


class InvalidArgument(FilesystemError, ValueError):
    """EINVAL class for non-path misuse: rename-into-subtree,
    readlink on a non-symlink, etc."""
    errno = _errno.EINVAL


class NotFound(FilesystemError, FileNotFoundError):
    errno = _errno.ENOENT


class AlreadyExists(FilesystemError, FileExistsError):
    errno = _errno.EEXIST


class NotADirectory(FilesystemError, NotADirectoryError):
    errno = _errno.ENOTDIR


class IsADirectory(FilesystemError, IsADirectoryError):
    errno = _errno.EISDIR


class DirectoryNotEmpty(FilesystemError, OSError):
    errno = _errno.ENOTEMPTY


class PermissionDenied(FilesystemError, PermissionError):
    errno = _errno.EACCES


class ReadOnlyFilesystem(FilesystemError, OSError):
    errno = _errno.EROFS


class NameTooLong(FilesystemError, OSError):
    errno = _errno.ENAMETOOLONG


class InvalidXattr(FilesystemError, OSError):
    """Bad xattr name, value too large, or namespace-related issue."""
    errno = _errno.EINVAL


class LockConflict(FilesystemError, BlockingIOError):
    errno = _errno.EAGAIN


class BadFileDescriptor(FilesystemError, OSError):
    errno = _errno.EBADF


class SymlinkLoop(FilesystemError, OSError):
    """Raised both on symlink chain > MAXSYMLINKS AND on
    open(..., O_NOFOLLOW) against a symlink. Both are ELOOP."""
    errno = _errno.ELOOP
