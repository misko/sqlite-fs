import pytest

from sqlite_fs.locks import LockManager
from sqlite_fs.types import LockOp, FlockOp
from sqlite_fs.errors import LockConflict


def test_posix_acquire_unconflicted():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
    q = lm.posix_getlk(inode=1, fd_id=10, pid=1000, start=0, length=0)
    # Same pid — no conflict, returns None (F_GETLK semantic).
    assert q is None
    # Cross-pid lookup sees the held lock.
    q2 = lm.posix_getlk(inode=1, fd_id=20, pid=2000, start=0, length=0)
    assert q2 is not None
    assert q2.type == "exclusive"
    assert q2.pid == 1000


def test_posix_shared_locks_coexist():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="shared", start=0, length=0)
    lm.posix_lock(inode=1, fd_id=20, pid=2000, op="shared", start=0, length=0)


def test_posix_exclusive_blocks_shared():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
    with pytest.raises(LockConflict):
        lm.posix_lock(inode=1, fd_id=20, pid=2000, op="shared", start=0, length=0)


def test_posix_shared_blocks_exclusive():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="shared", start=0, length=0)
    with pytest.raises(LockConflict):
        lm.posix_lock(inode=1, fd_id=20, pid=2000, op="exclusive", start=0, length=0)


def test_posix_same_process_upgrades():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="shared", start=0, length=0)
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
    # Same-pid upgrade: the cross-pid view sees exclusive.
    q = lm.posix_getlk(inode=1, fd_id=20, pid=2000, start=0, length=0)
    assert q is not None
    assert q.type == "exclusive"


def test_posix_range_lock_non_overlapping():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=100)
    lm.posix_lock(inode=1, fd_id=20, pid=2000, op="exclusive", start=100, length=100)


def test_posix_length_zero_means_infinity():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
    with pytest.raises(LockConflict):
        lm.posix_lock(inode=1, fd_id=20, pid=2000, op="shared",
                      start=(1 << 62), length=100)


def test_posix_close_any_fd_releases_all():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=100)
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=200, length=100)
    lm.on_fd_close(inode=1, fd_id=999, pid=1000)
    # Another process can now acquire both ranges.
    lm.posix_lock(inode=1, fd_id=20, pid=2000, op="exclusive", start=0, length=100)
    lm.posix_lock(inode=1, fd_id=20, pid=2000, op="exclusive", start=200, length=100)


def test_posix_unlock_releases_range():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=100)
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="unlock", start=0, length=100)
    lm.posix_lock(inode=1, fd_id=20, pid=2000, op="exclusive", start=0, length=100)


def test_ofd_scoped_by_fd_not_pid():
    lm = LockManager()
    lm.ofd_lock(inode=1, fd_id=10, op="exclusive", start=0, length=100)
    with pytest.raises(LockConflict):
        lm.ofd_lock(inode=1, fd_id=11, op="exclusive", start=0, length=100)


def test_ofd_close_releases_only_its_locks():
    lm = LockManager()
    lm.ofd_lock(inode=1, fd_id=10, op="exclusive", start=0, length=100)
    lm.ofd_lock(inode=1, fd_id=11, op="exclusive", start=200, length=100)
    lm.on_fd_close(inode=1, fd_id=10, pid=1000)
    # fd 10's range is free; fd 11's range is still locked.
    lm.ofd_lock(inode=1, fd_id=20, op="exclusive", start=0, length=100)
    with pytest.raises(LockConflict):
        lm.ofd_lock(inode=1, fd_id=21, op="exclusive", start=200, length=100)


def test_flock_whole_file_only():
    lm = LockManager()
    lm.flock(inode=1, fd_id=10, op="exclusive")
    with pytest.raises(LockConflict):
        lm.flock(inode=1, fd_id=11, op="exclusive")
    with pytest.raises(LockConflict):
        lm.flock(inode=1, fd_id=11, op="shared")


def test_flock_shared_coexist():
    lm = LockManager()
    lm.flock(inode=1, fd_id=10, op="shared")
    lm.flock(inode=1, fd_id=11, op="shared")
    lm.flock(inode=1, fd_id=12, op="shared")


def test_posix_and_flock_are_separate_namespaces():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
    lm.flock(inode=1, fd_id=20, op="exclusive")
    lm.ofd_lock(inode=1, fd_id=30, op="exclusive", start=0, length=0)


def test_wait_false_raises_immediately():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
    with pytest.raises(LockConflict):
        lm.posix_lock(inode=1, fd_id=20, pid=2000, op="exclusive",
                      start=0, length=0, wait=False)


def test_getlk_returns_none_if_free():
    lm = LockManager()
    assert lm.posix_getlk(inode=1, fd_id=10, pid=1000, start=0, length=0) is None


def test_getlk_returns_conflicting_lock():
    lm = LockManager()
    lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=100, length=50)
    q = lm.posix_getlk(inode=1, fd_id=20, pid=2000, start=0, length=200)
    assert q is not None
    assert q.type == "exclusive"
    assert q.pid == 1000
    assert q.start == 100
    assert q.length == 50
