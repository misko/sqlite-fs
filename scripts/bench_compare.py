#!/usr/bin/env python3
"""Side-by-side benchmark: sqlite-fs-through-FUSE vs the host partition (ext4).

Two workloads:

  SMALL FILES — create 5000 files of 4 KiB each, stat each, read each,
                unlink each. Exercises per-op overhead (open/write/stat/
                read/close/unlink round-trips).

  BIG FILES   — create 3 files of 128 MiB each, sequentially written and
                then read back. Exercises throughput and fsync cost.

Reported per phase: wall-clock time and per-op or throughput metric,
side by side for ext4 and sqlite-fs.
"""
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from sqlite_fs import mkfs


SMALL_COUNT = int(os.environ.get("BENCH_SMALL_COUNT", "5000"))
SMALL_SIZE = int(os.environ.get("BENCH_SMALL_SIZE", str(4 * 1024)))

BIG_COUNT = int(os.environ.get("BENCH_BIG_COUNT", "3"))
BIG_SIZE = int(os.environ.get("BENCH_BIG_SIZE", str(128 * 1024 * 1024)))
BIG_CHUNK = 1 << 20  # 1 MiB per write in the big-file phase


@contextmanager
def timer():
    start = time.perf_counter()
    result = {}
    yield result
    result["elapsed"] = time.perf_counter() - start


def run_workload(root, label):
    """Run the full workload inside directory `root`. Return a dict of timings."""
    print(f"\n===== {label}: {root} =====")
    small_dir = os.path.join(root, "small")
    big_dir = os.path.join(root, "big")
    os.makedirs(small_dir, exist_ok=True)
    os.makedirs(big_dir, exist_ok=True)

    results = {"label": label}

    # Small files: create+write
    payload = b"x" * SMALL_SIZE
    with timer() as t:
        for i in range(SMALL_COUNT):
            with open(os.path.join(small_dir, f"f{i:05d}"), "wb") as f:
                f.write(payload)
    results["small_create_s"] = t["elapsed"]
    rate = SMALL_COUNT / t["elapsed"]
    print(f"  {SMALL_COUNT:5d} × create+write 4 KiB  : {t['elapsed']:7.2f} s  ({rate:.0f} ops/s)")

    # Small files: stat
    with timer() as t:
        for i in range(SMALL_COUNT):
            os.stat(os.path.join(small_dir, f"f{i:05d}"))
    results["small_stat_s"] = t["elapsed"]
    print(f"  {SMALL_COUNT:5d} × stat                : {t['elapsed']:7.2f} s  ({SMALL_COUNT / t['elapsed']:.0f} ops/s)")

    # Small files: read
    with timer() as t:
        for i in range(SMALL_COUNT):
            with open(os.path.join(small_dir, f"f{i:05d}"), "rb") as f:
                f.read()
    results["small_read_s"] = t["elapsed"]
    print(f"  {SMALL_COUNT:5d} × open+read+close     : {t['elapsed']:7.2f} s  ({SMALL_COUNT / t['elapsed']:.0f} ops/s)")

    # Small files: unlink
    with timer() as t:
        for i in range(SMALL_COUNT):
            os.unlink(os.path.join(small_dir, f"f{i:05d}"))
    results["small_unlink_s"] = t["elapsed"]
    print(f"  {SMALL_COUNT:5d} × unlink              : {t['elapsed']:7.2f} s  ({SMALL_COUNT / t['elapsed']:.0f} ops/s)")

    # Big files: sequential write
    big_total = BIG_COUNT * BIG_SIZE
    with timer() as t:
        chunk = b"x" * BIG_CHUNK
        for i in range(BIG_COUNT):
            with open(os.path.join(big_dir, f"b{i}.bin"), "wb") as f:
                for _ in range(BIG_SIZE // BIG_CHUNK):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
    results["big_write_s"] = t["elapsed"]
    thru = big_total / (1024 * 1024) / t["elapsed"]
    print(f"  {BIG_COUNT:5d} × seq write {BIG_SIZE//(1024*1024)} MiB + fsync: {t['elapsed']:7.2f} s  ({thru:.1f} MiB/s)")

    # Big files: sequential read (after drop-caches? we can't, but the
    # kernel page cache may still have the data — report anyway).
    with timer() as t:
        for i in range(BIG_COUNT):
            with open(os.path.join(big_dir, f"b{i}.bin"), "rb") as f:
                while True:
                    buf = f.read(BIG_CHUNK)
                    if not buf:
                        break
    results["big_read_s"] = t["elapsed"]
    thru = big_total / (1024 * 1024) / t["elapsed"]
    print(f"  {BIG_COUNT:5d} × seq read  {BIG_SIZE//(1024*1024)} MiB      : {t['elapsed']:7.2f} s  ({thru:.1f} MiB/s)")

    # Big files: unlink
    with timer() as t:
        for i in range(BIG_COUNT):
            os.unlink(os.path.join(big_dir, f"b{i}.bin"))
    results["big_unlink_s"] = t["elapsed"]
    print(f"  {BIG_COUNT:5d} × unlink big          : {t['elapsed']:7.2f} s")

    return results


def run_host_workload():
    tmpdir = tempfile.mkdtemp(prefix="hostbench-")
    try:
        return run_workload(tmpdir, f"HOST ext4 ({tmpdir})")
    finally:
        subprocess.run(["rm", "-rf", tmpdir], check=False)


def run_sqlite_fs_workload(sync_mode="full"):
    workdir = tempfile.mkdtemp(prefix=f"sqlitebench-{sync_mode}-")
    db = os.path.join(workdir, "bench.db")
    mnt = os.path.join(workdir, "mnt")
    os.makedirs(mnt)
    mkfs(db)

    stderr_log = os.path.join(workdir, "daemon.stderr")
    proc = subprocess.Popen(
        ["sqlite-fs", "mount", db, mnt,
         "--foreground", "--sync-mode", sync_mode],
        stderr=open(stderr_log, "wb"),
    )
    for _ in range(100):
        with open("/proc/self/mountinfo") as f:
            if mnt in f.read():
                break
        time.sleep(0.05)
    else:
        proc.kill(); proc.wait()
        sys.exit("mount failed")

    try:
        result = run_workload(mnt, f"SQLITE-FS sync={sync_mode} ({db})")
        result["db_size_bytes"] = 0
    except Exception:
        print("\n--- daemon stderr ---")
        with open(stderr_log) as f:
            tail = f.read()
        print(tail[-4000:] if len(tail) > 4000 else tail)
        print("--- end stderr ---")
        raise
    finally:
        subprocess.run(["fusermount3", "-u", mnt], check=False)
        proc.wait(timeout=30)

    result["db_size_bytes"] = os.path.getsize(db)
    result["sync_mode"] = sync_mode
    subprocess.run(["rm", "-rf", workdir], check=False)
    return result


def print_comparison(host, sqlitefs):
    print("\n" + "=" * 72)
    print("COMPARISON (sqlite-fs / host ratio; lower is faster for sqlite-fs)")
    print("=" * 72)

    def fmt(key, unit, precision=2):
        h = host[key]
        s = sqlitefs[key]
        ratio = s / h if h > 0 else float("inf")
        return (f"  {key:25s}  host={h:8.{precision}f} {unit:6s}  "
                f"sqlite-fs={s:8.{precision}f} {unit:6s}  ratio={ratio:5.2f}x")

    keys = [
        ("small_create_s", "s"),
        ("small_stat_s",   "s"),
        ("small_read_s",   "s"),
        ("small_unlink_s", "s"),
        ("big_write_s",    "s"),
        ("big_read_s",     "s"),
        ("big_unlink_s",   "s"),
    ]
    for k, u in keys:
        print(fmt(k, u))

    print()
    big_total_mib = BIG_COUNT * BIG_SIZE // (1024 * 1024)
    print(f"  big seq write throughput   host="
          f"{big_total_mib/host['big_write_s']:6.1f} MiB/s   "
          f"sqlite-fs={big_total_mib/sqlitefs['big_write_s']:6.1f} MiB/s")
    print(f"  big seq read  throughput   host="
          f"{big_total_mib/host['big_read_s']:6.1f} MiB/s   "
          f"sqlite-fs={big_total_mib/sqlitefs['big_read_s']:6.1f} MiB/s")

    print()
    print(f"  sqlite-fs db size after bench: {sqlitefs['db_size_bytes']/1024/1024:.1f} MiB "
          f"(content alone: ~{(BIG_COUNT*BIG_SIZE)/1024/1024:.0f} MiB)")


def print_three_way(host, full, normal):
    keys = [
        ("small_create_s", "s"),
        ("small_stat_s",   "s"),
        ("small_read_s",   "s"),
        ("small_unlink_s", "s"),
        ("big_write_s",    "s"),
        ("big_read_s",     "s"),
    ]
    print("\n" + "=" * 86)
    print(f"{'operation':22s} {'host ext4':>14s} {'sqlite-fs=full':>18s} "
          f"{'sqlite-fs=normal':>18s} {'normal/full':>10s}")
    print("=" * 86)
    for k, _ in keys:
        h = host[k]; f = full[k]; n = normal[k]
        speedup = f / n if n > 0 else float("inf")
        print(f"{k:22s} {h:12.4f} s  {f:16.4f} s  {n:16.4f} s  "
              f"{speedup:9.2f}x")

    big_mib = BIG_COUNT * BIG_SIZE // (1024 * 1024)
    print()
    print(f"big seq write:  host={big_mib/host['big_write_s']:6.1f} MiB/s   "
          f"sync=full={big_mib/full['big_write_s']:6.1f} MiB/s   "
          f"sync=normal={big_mib/normal['big_write_s']:6.1f} MiB/s")
    print(f"big seq read :  host={big_mib/host['big_read_s']:6.1f} MiB/s   "
          f"sync=full={big_mib/full['big_read_s']:6.1f} MiB/s   "
          f"sync=normal={big_mib/normal['big_read_s']:6.1f} MiB/s")


def main():
    print(f"Workload: {SMALL_COUNT} small files × {SMALL_SIZE} bytes,  "
          f"{BIG_COUNT} big files × {BIG_SIZE//(1024*1024)} MiB")

    host = run_host_workload()
    full = run_sqlite_fs_workload(sync_mode="full")
    normal = run_sqlite_fs_workload(sync_mode="normal")
    print_three_way(host, full, normal)


if __name__ == "__main__":
    main()
