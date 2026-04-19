try:
    import pyfuse3  # noqa: F401
except ImportError as e:
    raise ImportError(
        "sqlite_fs.fuse requires pyfuse3. "
        "Install with: pip install sqlite-fs[fuse]"
    ) from e

from sqlite_fs.fuse.adapter import Adapter, mount, umount

__all__ = ["Adapter", "mount", "umount"]
