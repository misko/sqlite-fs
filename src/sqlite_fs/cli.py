import argparse
import os
import sys

from sqlite_fs import mkfs as _mkfs, open_fs


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sqlite-fs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mkfs = sub.add_parser("mkfs", help="create a new sqlite-fs DB")
    p_mkfs.add_argument("path")
    p_mkfs.add_argument("--chunk-size", type=int, default=65536)
    p_mkfs.add_argument("--overwrite", action="store_true")

    p_mount = sub.add_parser("mount", help="mount an sqlite-fs DB")
    p_mount.add_argument("db")
    p_mount.add_argument("mountpoint")
    p_mount.add_argument("--foreground", action="store_true")
    p_mount.add_argument("--readonly", action="store_true")
    p_mount.add_argument("--subdir", default=None)
    p_mount.add_argument(
        "--sync-mode", default="full",
        choices=["full", "normal", "off"],
        help=("SQLite synchronous PRAGMA. 'full' = fsync per commit, "
              "idea.md durability contract. 'normal' = WAL-safe; may "
              "lose last txn on power loss. 'off' = dangerous; scratch only."),
    )

    p_umount = sub.add_parser("umount",
                              help="unmount an sqlite-fs mountpoint")
    p_umount.add_argument("mountpoint")

    p_fsck = sub.add_parser("fsck", help="check filesystem integrity")
    p_fsck.add_argument("path")

    p_export = sub.add_parser(
        "export", help="write filesystem contents to a host directory")
    p_export.add_argument("db")
    p_export.add_argument("dest")

    args = parser.parse_args(argv)

    if args.cmd == "mkfs":
        _mkfs(args.path, chunk_size=args.chunk_size, overwrite=args.overwrite)
        return 0

    if args.cmd in ("mount", "umount"):
        try:
            from sqlite_fs.fuse.cli import mount_cmd, umount_cmd
        except ImportError as e:
            sys.stderr.write(
                f"sqlite-fs: FUSE support requires pyfuse3.\n"
                f"Install with: pip install sqlite-fs[fuse]\n"
                f"Details: {e}\n"
            )
            return 3
        if args.cmd == "mount":
            return mount_cmd(args)
        return umount_cmd(args)

    if args.cmd == "fsck":
        with open_fs(args.path, readonly=True) as fs:
            report = fs.fsck()
        print(f"integrity_check: {report.integrity_check_result}")
        for issue in report.issues:
            print(f"  {issue.kind}: {issue.detail}")
        return 0 if (report.integrity_check_result == "ok"
                     and not report.issues) else 1

    if args.cmd == "export":
        return _export(args.db, args.dest)

    return 2


def _export(db_path, dest):
    os.makedirs(dest, exist_ok=True)
    with open_fs(db_path, readonly=True) as fs:
        _export_dir(fs, "/", dest)
    return 0


def _export_dir(fs, src_path, dest_path):
    for entry in fs.readdir(src_path):
        sub_src = (src_path.rstrip("/") + "/" + entry.name
                   if src_path != "/" else "/" + entry.name)
        sub_dest = os.path.join(dest_path, entry.name)
        if entry.kind == "dir":
            os.makedirs(sub_dest, exist_ok=True)
            _export_dir(fs, sub_src, sub_dest)
        elif entry.kind == "symlink":
            target = fs.readlink(sub_src)
            os.symlink(target, sub_dest)
        else:
            fd = fs.open(sub_src, flags=0)
            try:
                with open(sub_dest, "wb") as f:
                    offset = 0
                    while True:
                        chunk = fs.read(fd, size=64 * 1024, offset=offset)
                        if not chunk:
                            break
                        f.write(chunk)
                        offset += len(chunk)
            finally:
                fs.close_fd(fd)
        st = fs.stat(sub_src, follow_symlinks=False)
        if entry.kind != "symlink":
            os.chmod(sub_dest, st.mode & 0o7777)


if __name__ == "__main__":
    raise SystemExit(main())
