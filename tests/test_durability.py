"""Durability and invariant tests. Includes a hypothesis stateful machine
that applies random sequences of operations and checks that every
observable invariant from idea.md § Invariants holds after each step.
"""
import os
import sqlite3
import pytest

from hypothesis import settings, Verbosity
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize
from hypothesis import strategies as st

from sqlite_fs import mkfs, open_fs


# ---------- Simple reopen roundtrip tests ----------

def test_reopen_preserves_directory_tree(tmp_db):
    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fs.mkdir("/a")
        fs.mkdir("/a/b")
        fs.mkdir("/a/b/c")
    fs.close()

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        assert [e.name for e in fs.readdir("/")] == ["a"]
        assert [e.name for e in fs.readdir("/a")] == ["b"]
        assert [e.name for e in fs.readdir("/a/b")] == ["c"]
    fs.close()


def test_integrity_check_ok_after_many_ops(fresh_fs):
    with fresh_fs.as_user(0, 0):
        for i in range(100):
            fresh_fs.mkdir(f"/dir{i}")
        for i in range(100):
            fd = fresh_fs.create(f"/dir{i}/file", mode=0o644)
            fresh_fs.write(fd, f"content{i}".encode(), offset=0)
            fresh_fs.close_fd(fd)
        for i in range(50):
            fresh_fs.setxattr(f"/dir{i}/file", "user.idx", str(i).encode())
        for i in range(50):
            fresh_fs.symlink(f"/dir{i}/file".encode(), f"/link{i}")
        for i in range(50, 100):
            fresh_fs.unlink(f"/dir{i}/file")
            fresh_fs.rmdir(f"/dir{i}")

    report = fresh_fs.fsck()
    assert report.integrity_check_result == "ok"
    assert report.issues == []


def test_wal_mode_is_on(tmp_db):
    mkfs(str(tmp_db))
    conn = sqlite3.connect(str(tmp_db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert mode == ("wal",)


def test_synchronous_full(tmp_db):
    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    assert fs._sqlite_pragma("synchronous") == 2
    fs.close()


def test_chunk_size_preserved_across_reopen(tmp_db):
    mkfs(str(tmp_db), chunk_size=32768)
    fs = open_fs(str(tmp_db))
    assert fs._chunk_size() == 32768
    fs.close()

    fs = open_fs(str(tmp_db))
    assert fs._chunk_size() == 32768
    fs.close()


# ---------- Hypothesis stateful: random ops, invariants hold ----------

class FsStateMachine(RuleBasedStateMachine):
    """Applies random filesystem operations and checks invariants after each.

    We model a flat namespace for simplicity (no nested dirs in the
    state machine — the library handles nested; we just need a shape
    that stresses create/unlink/rename/link interactions)."""

    @initialize()
    def setup(self):
        self._tmpdir = __import__("tempfile").mkdtemp(prefix="sqlite-fs-hyp-")
        self._db = os.path.join(self._tmpdir, "fs.db")
        mkfs(self._db)
        self._fs = open_fs(self._db)
        self._fs.as_user(0, 0).__enter__()
        # Track what we think should exist at each path → inode.
        self._expected = {}

    def teardown(self):
        if hasattr(self, "_fs"):
            self._fs.close()

    # --- rules ---

    names = st.text(alphabet="abcde", min_size=1, max_size=3)

    @rule(name=names)
    def do_create(self, name):
        path = f"/{name}"
        if path in self._expected:
            return
        fd = self._fs.create(path)
        self._fs.write(fd, b"x" * 32, 0)
        self._fs.close_fd(fd)
        inode = self._fs.stat(path).inode
        self._expected[path] = {"kind": "file", "inode": inode, "size": 32}

    @rule(src=names, dst=names)
    def do_rename(self, src, dst):
        src_path, dst_path = f"/{src}", f"/{dst}"
        if src_path not in self._expected:
            return
        if src_path == dst_path:
            return
        if dst_path in self._expected:
            if self._expected[dst_path]["kind"] == "dir":
                return
        self._fs.rename(src_path, dst_path)
        meta = self._expected.pop(src_path)
        self._expected[dst_path] = meta

    @rule(src=names, dst=names)
    def do_link(self, src, dst):
        src_path, dst_path = f"/{src}", f"/{dst}"
        if src_path not in self._expected:
            return
        if self._expected[src_path]["kind"] != "file":
            return
        if dst_path in self._expected:
            return
        self._fs.link(src_path, dst_path)
        self._expected[dst_path] = dict(self._expected[src_path])

    @rule(name=names)
    def do_unlink(self, name):
        path = f"/{name}"
        if path not in self._expected:
            return
        if self._expected[path]["kind"] == "dir":
            return
        self._fs.unlink(path)
        del self._expected[path]

    # --- invariants ---

    @invariant()
    def integrity_ok(self):
        if not hasattr(self, "_fs"):
            return
        report = self._fs.fsck()
        assert report.integrity_check_result == "ok", (
            f"integrity violated: {report.issues}"
        )

    @invariant()
    def size_matches_blob(self):
        if not hasattr(self, "_fs"):
            return
        for path, meta in self._expected.items():
            if meta["kind"] != "file":
                continue
            st = self._fs.stat(path)
            assert st.size == meta["size"]

    @invariant()
    def readdir_matches_expected(self):
        if not hasattr(self, "_fs"):
            return
        actual = {e.name for e in self._fs.readdir("/")}
        expected = {p.lstrip("/") for p in self._expected.keys()}
        assert actual == expected, f"tree mismatch: actual={actual}, expected={expected}"


# Configure hypothesis for a short, bounded run (we don't want minutes).
settings.register_profile(
    "fast-stateful",
    max_examples=25,
    stateful_step_count=20,
    deadline=None,
    verbosity=Verbosity.normal,
)
settings.load_profile("fast-stateful")

TestFsStateMachine = FsStateMachine.TestCase
