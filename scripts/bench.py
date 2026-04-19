#!/usr/bin/env python3
"""Performance benchmarks for sqlite-fs.

Runs a fixed workload both library-direct and through the FUSE mount
when possible, reports per-op time and throughput. Not a test — just a
profile we read. Soft bounds from idea.md are informational; printing
the measurements is what matters.
"""
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from sqlite_fs import mkfs, open_fs


@contextmanager
def timer(name):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    print(f"  {name:45s} {elapsed * 1000:10.2f} ms  ({elapsed:.3f}s)")


def bench_library(db_path):
    print("== LIBRARY-DIRECT ==")
    mkfs(db_path, overwrite=True)

    with open_fs(db_path) as fs:
        with fs.as_user(0, 0):
            # Setup.
            fs.mkdir("/bench")

            # mkdir × 1000
            with timer("1000 × mkdir"):
                for i in range(1000):
                    fs.mkdir(f"/bench/d{i:04d}")

            # stat × 10000 hot
            for i in range(10):   # warm
                fs.stat(f"/bench/d0000")
            with timer("10000 × stat hot"):
                for _ in range(10000):
                    fs.stat("/bench/d0000")

            # create + write 4K × 1000
            with timer("1000 × create+write 4K+close"):
                for i in range(1000):
                    fd = fs.create(f"/bench/d{i:04d}/f")
                    fs.write(fd, b"x" * 4096, 0)
                    fs.close_fd(fd)

            # read 4K hot × 10000
            fd = fs.open("/bench/d0000/f", flags=0)
            with timer("10000 × read 4K hot"):
                for _ in range(10000):
                    fs.read(fd, size=4096, offset=0)
            fs.close_fd(fd)

            # sequential write 16 MiB
            CHUNK = 1 << 16
            fd = fs.create("/bench/seq.bin")
            payload = b"x" * CHUNK
            with timer("sequential write 16 MiB (16x1MiB)"):
                for i in range(256):   # 256 × 64 KiB = 16 MiB
                    fs.write(fd, payload, offset=i * CHUNK)
            fs.close_fd(fd)

            # sequential read 16 MiB
            fd = fs.open("/bench/seq.bin", flags=0)
            with timer("sequential read 16 MiB"):
                total = 0
                offset = 0
                while True:
                    buf = fs.read(fd, size=CHUNK, offset=offset)
                    if not buf:
                        break
                    total += len(buf)
                    offset += len(buf)
            fs.close_fd(fd)

            # readdir 1000 entries
            with timer("readdir 1000 entries"):
                entries = fs.readdir("/bench")
            assert len(entries) == 1001   # 1000 d* + 1 seq.bin

    size = os.path.getsize(db_path)
    print(f"  db size after bench: {size/1024/1024:.1f} MiB")


def bench_fuse(db_path, mountpoint):
    print("== THROUGH-FUSE ==")
    mkfs(db_path, overwrite=True)
    os.makedirs(mountpoint, exist_ok=True)

    proc = subprocess.Popen(
        ["sqlite-fs", "mount", db_path, mountpoint, "--foreground"],
    )
    for _ in range(100):
        with open("/proc/self/mountinfo") as f:
            if mountpoint in f.read():
                break
        time.sleep(0.05)
    else:
        proc.kill(); proc.wait()
        sys.exit("mount failed")

    try:
        bench_dir = os.path.join(mountpoint, "bench")
        os.makedirs(bench_dir)

        # mkdir × 200 (smaller — FUSE round-trips are slow)
        with timer("200 × mkdir (FUSE)"):
            for i in range(200):
                os.makedirs(os.path.join(bench_dir, f"d{i:03d}"))

        # stat × 1000 hot
        for _ in range(10):
            os.stat(os.path.join(bench_dir, "d000"))
        with timer("1000 × stat hot (FUSE)"):
            for _ in range(1000):
                os.stat(os.path.join(bench_dir, "d000"))

        # create+write 4K × 200
        with timer("200 × open+write 4K+close (FUSE)"):
            for i in range(200):
                fd = os.open(
                    os.path.join(bench_dir, f"d{i:03d}/f"),
                    os.O_CREAT | os.O_WRONLY, 0o644,
                )
                os.write(fd, b"x" * 4096)
                os.close(fd)

        # sequential write 16 MiB
        seq = os.path.join(bench_dir, "seq.bin")
        CHUNK = 1 << 16
        payload = b"x" * CHUNK
        with timer("sequential write 16 MiB (FUSE)"):
            with open(seq, "wb") as f:
                for _ in range(256):
                    f.write(payload)

        # sequential read 16 MiB
        with timer("sequential read 16 MiB (FUSE)"):
            with open(seq, "rb") as f:
                total = 0
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        break
                    total += len(buf)

        # readdir
        with timer("readdir 200 entries (FUSE)"):
            _ = os.listdir(bench_dir)

    finally:
        subprocess.run(["fusermount3", "-u", mountpoint], check=False)
        proc.wait(timeout=5)


def main():
    workdir = tempfile.mkdtemp(prefix="sqlite-fs-bench-")
    db = os.path.join(workdir, "bench.db")

    bench_library(db)
    print()
    mnt = os.path.join(workdir, "mnt")
    bench_fuse(db, mnt)

    print(f"\nScratch: {workdir}")


if __name__ == "__main__":
    main()
