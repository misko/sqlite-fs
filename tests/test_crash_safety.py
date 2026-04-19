"""Crash safety: SIGKILL the daemon mid-write, remount, verify no corruption
and no lost committed transactions.
"""
import os
import signal
import subprocess
import sys
import sqlite3
import tempfile
import time
import pytest

from sqlite_fs import mkfs, open_fs


def _spawn_writer(db_path: str) -> subprocess.Popen:
    """Spawn a Python subprocess that opens the fs, reads ops from stdin,
    executes them, acknowledges on stdout. Used to simulate a daemon that
    we can SIGKILL mid-operation."""
    script = """
import sys, time
from sqlite_fs import open_fs

db_path = sys.argv[1]
with open_fs(db_path) as fs:
    with fs.as_user(0, 0):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            op, *args = line.split(" ", 1)
            try:
                if op == "mkdir":
                    fs.mkdir(args[0])
                elif op == "write":
                    # 'write /path payload'
                    path, payload = args[0].split(" ", 1)
                    fd = fs.create(path)
                    fs.write(fd, payload.encode(), 0)
                    fs.close_fd(fd)
                elif op == "rename":
                    src, dst = args[0].split(" ", 1)
                    fs.rename(src, dst)
                elif op == "ack":
                    sys.stdout.write("OK\\n"); sys.stdout.flush()
                elif op == "sleep":
                    time.sleep(float(args[0]))
                elif op == "exit":
                    break
            except Exception as e:
                sys.stdout.write(f"ERR {type(e).__name__} {e}\\n"); sys.stdout.flush()
"""
    return subprocess.Popen(
        [sys.executable, "-c", script, db_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def _send(proc, cmd, wait_ack=True):
    proc.stdin.write(cmd + "\n")
    if wait_ack:
        proc.stdin.write("ack\n")
    proc.stdin.flush()
    if wait_ack:
        line = proc.stdout.readline().strip()
        assert line == "OK", f"unexpected: {line}"


@pytest.fixture
def tmp_dbfile(tmp_path):
    return str(tmp_path / "crash.db")


def test_integrity_check_ok_after_sigkill(tmp_dbfile):
    mkfs(tmp_dbfile)
    proc = _spawn_writer(tmp_dbfile)
    try:
        _send(proc, "mkdir /a")
        _send(proc, "write /a/x.txt hello")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()

    # Reopen; PRAGMA integrity_check must return 'ok'.
    conn = sqlite3.connect(tmp_dbfile)
    r = conn.execute("PRAGMA integrity_check").fetchone()
    conn.close()
    assert r == ("ok",)


def test_committed_writes_survive_sigkill(tmp_dbfile):
    mkfs(tmp_dbfile)
    proc = _spawn_writer(tmp_dbfile)
    try:
        _send(proc, "write /committed.txt persisted-data")
        # write_and_ack means: the write returned success to the subprocess.
        # The write has been committed (synchronous=FULL fsyncs the WAL).
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()

    with open_fs(tmp_dbfile) as fs:
        with fs.as_user(0, 0):
            fd = fs.open("/committed.txt", flags=0)
            assert fs.read(fd, size=100, offset=0) == b"persisted-data"
            fs.close_fd(fd)


def test_half_written_rename_is_atomic_under_crash(tmp_dbfile):
    """rename(src, dst) commits in a single SQLite transaction. After crash,
    exactly one of src/dst exists, never both/neither."""
    mkfs(tmp_dbfile)

    # Prepare: create src, ack.
    proc = _spawn_writer(tmp_dbfile)
    _send(proc, "write /src content-x")
    _send(proc, "exit", wait_ack=False)
    proc.stdin.close()
    proc.wait(timeout=2)

    # Racing rename: spawn a new process, send rename, immediately SIGKILL.
    proc = _spawn_writer(tmp_dbfile)
    try:
        proc.stdin.write("rename /src /dst\n")
        proc.stdin.flush()
        # Don't wait for ack; race.
        time.sleep(0.001)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()

    # After crash, exactly one of src/dst exists. Content preserved at winner.
    with open_fs(tmp_dbfile) as fs:
        with fs.as_user(0, 0):
            src_there = fs.exists("/src")
            dst_there = fs.exists("/dst")
            assert (src_there, dst_there) in [(True, False), (False, True)], \
                f"atomicity violation: src={src_there}, dst={dst_there}"
            path = "/src" if src_there else "/dst"
            fd = fs.open(path, flags=0)
            assert fs.read(fd, size=100, offset=0) == b"content-x"
            fs.close_fd(fd)


def test_remount_is_idempotent_after_crash(tmp_dbfile):
    mkfs(tmp_dbfile)
    proc = _spawn_writer(tmp_dbfile)
    _send(proc, "mkdir /persistent")
    _send(proc, "write /x.txt hello")
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=2)

    # Mount and read twice.
    for attempt in range(2):
        with open_fs(tmp_dbfile) as fs:
            with fs.as_user(0, 0):
                assert fs.exists("/persistent")
                fd = fs.open("/x.txt", flags=0)
                assert fs.read(fd, size=100, offset=0) == b"hello"
                fs.close_fd(fd)


def test_fsck_ok_after_repeated_crashes(tmp_dbfile):
    """Crash, reopen, write more, crash again. Integrity holds across
    multiple crash-remount cycles."""
    mkfs(tmp_dbfile)

    for cycle in range(3):
        proc = _spawn_writer(tmp_dbfile)
        try:
            for i in range(5):
                _send(proc, f"write /cycle{cycle}_file{i}.txt data-{cycle}-{i}")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=2)
        finally:
            if proc.poll() is None:
                proc.kill()

    with open_fs(tmp_dbfile) as fs:
        report = fs.fsck()
        assert report.integrity_check_result == "ok"
        assert report.issues == []
        with fs.as_user(0, 0):
            # All 15 files should be present (3 cycles × 5 files).
            names = [e.name for e in fs.readdir("/") if e.kind == "file"]
            assert len(names) == 15
