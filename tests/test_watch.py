import pytest

from sqlite_fs import Event


def test_create_fires_create_event(as_root):
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.mkdir("/newdir")
        first = next(events)
        assert first.kind == "create"
        assert first.path == "/newdir"
        assert first.node_kind == "dir"
        assert first.inode == as_root.stat("/newdir").inode


def test_unlink_fires_remove_event(as_root):
    fd = as_root.create("/f"); as_root.close_fd(fd)
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.unlink("/f")
        ev = next(events)
        assert ev.kind == "remove"
        assert ev.path == "/f"
        assert ev.node_kind == "file"


def test_write_fires_modify_event(as_root):
    fd = as_root.create("/f")
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.write(fd, b"hello", offset=0)
        ev = next(events)
        assert ev.kind == "modify"
        assert ev.path == "/f"
        assert ev.node_kind == "file"
    as_root.close_fd(fd)


def test_rename_fires_move_event(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.rename("/a", "/b")
        ev = next(events)
        assert ev.kind == "move"
        assert ev.src_path == "/a"
        assert ev.dst_path == "/b"
        assert ev.path == "/b"


def test_rename_exchange_fires_two_move_events(as_root):
    fd = as_root.create("/a"); as_root.close_fd(fd)
    fd = as_root.create("/b"); as_root.close_fd(fd)
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.rename("/a", "/b", exchange=True)
        e1 = next(events)
        e2 = next(events)
        assert e1.kind == "move"
        assert e1.src_path == "/a"
        assert e1.dst_path == "/b"
        assert e2.kind == "move"
        assert e2.src_path == "/b"
        assert e2.dst_path == "/a"


def test_rolled_back_mutation_does_not_emit(as_root):
    from sqlite_fs import AlreadyExists
    as_root.mkdir("/existing")
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        with pytest.raises(AlreadyExists):
            as_root.mkdir("/existing")
        as_root.mkdir("/next")
        ev = next(events)
        assert ev.path == "/next"
        assert ev.kind == "create"


def test_watcher_sees_events_only_after_construction(as_root):
    as_root.mkdir("/before_watch")
    with as_root.watch("/", recursive=False) as w:
        events = iter(w)
        as_root.mkdir("/after_watch")
        ev = next(events)
        assert ev.path == "/after_watch"


def test_recursive_watch_sees_descendants(as_root):
    as_root.mkdir("/top")
    with as_root.watch("/top", recursive=True) as w:
        events = iter(w)
        as_root.mkdir("/top/sub")
        as_root.mkdir("/top/sub/deep")
        e1 = next(events)
        e2 = next(events)
        assert e1.path == "/top/sub"
        assert e2.path == "/top/sub/deep"


def test_non_recursive_watch_ignores_descendants(as_root):
    as_root.mkdir("/top")
    with as_root.watch("/top", recursive=False) as w:
        events = iter(w)
        as_root.mkdir("/top/sub")
        as_root.mkdir("/top/sub/deep")
        as_root.mkdir("/top/sibling")
        e1 = next(events)
        e2 = next(events)
        assert e1.path == "/top/sub"
        assert e2.path == "/top/sibling"


def test_watcher_iteration_after_close_stops(as_root):
    w = as_root.watch("/", recursive=False)
    as_root.mkdir("/a")
    events = iter(w)
    first = next(events)
    assert first.path == "/a"
    w.close()
    assert list(events) == []
