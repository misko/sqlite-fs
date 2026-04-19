import os
import sys


def mount_cmd(args):
    from sqlite_fs.fuse import mount

    if not os.path.exists(args.db):
        sys.stderr.write(f"sqlite-fs: no such DB: {args.db}\n")
        return 1
    if not os.path.isdir(args.mountpoint):
        sys.stderr.write(
            f"sqlite-fs: mountpoint not a directory: {args.mountpoint}\n"
        )
        return 1

    try:
        mount(
            args.db,
            args.mountpoint,
            foreground=args.foreground,
            readonly=args.readonly,
            subdir=getattr(args, "subdir", None),
            sync_mode=getattr(args, "sync_mode", "full"),
        )
        return 0
    except Exception as e:
        sys.stderr.write(f"sqlite-fs: mount failed: {e}\n")
        return 1


def umount_cmd(args):
    from sqlite_fs.fuse import umount
    try:
        umount(args.mountpoint)
        return 0
    except Exception as e:
        sys.stderr.write(f"sqlite-fs: umount failed: {e}\n")
        return 1
