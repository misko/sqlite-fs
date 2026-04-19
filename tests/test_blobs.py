import pytest

from sqlite_fs.errors import BadFileDescriptor


CHUNK = 64 * 1024


@pytest.fixture
def fs_with_file(as_root):
    fd = as_root.create("/f", mode=0o644)
    try:
        yield as_root, fd
    finally:
        try:
            as_root.close_fd(fd)
        except BadFileDescriptor:
            pass


def test_write_and_read_small(fs_with_file):
    fs, fd = fs_with_file
    written = fs.write(fd, b"hello", offset=0)
    assert written == 5
    assert fs.read(fd, size=5, offset=0) == b"hello"
    assert fs.stat("/f").size == 5


def test_write_within_one_chunk_uses_one_chunk_row(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"x" * 1000, offset=0)
    assert fs.stat("/f").size == 1000
    assert fs._count_chunks(fs.stat("/f").inode) == 1


def test_write_across_chunk_boundary(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"a" * 100, offset=CHUNK - 50)
    assert fs.stat("/f").size == CHUNK - 50 + 100
    assert fs._count_chunks(fs.stat("/f").inode) == 2

    data = fs.read(fd, size=100, offset=CHUNK - 50)
    assert data == b"a" * 100


def test_read_past_eof_returns_empty(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"hello", offset=0)
    assert fs.read(fd, size=100, offset=1000) == b""


def test_read_partial_past_eof_returns_truncated(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"hello", offset=0)
    assert fs.read(fd, size=100, offset=2) == b"llo"


def test_write_past_eof_zero_pads_gap(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"end", offset=100)
    assert fs.stat("/f").size == 103
    assert fs.read(fd, size=100, offset=0) == b"\x00" * 100
    assert fs.read(fd, size=3, offset=100) == b"end"


def test_truncate_to_zero_removes_all_chunks(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"x" * (CHUNK * 3), offset=0)
    assert fs._count_chunks(fs.stat("/f").inode) == 3

    fs.truncate_fd(fd, 0)
    assert fs.stat("/f").size == 0
    assert fs._count_chunks(fs.stat("/f").inode) == 0
    assert fs.read(fd, size=100, offset=0) == b""


def test_truncate_shrink_removes_trailing_chunks(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"x" * (CHUNK * 3 + 100), offset=0)
    assert fs._count_chunks(fs.stat("/f").inode) == 4

    fs.truncate_fd(fd, CHUNK + 500)
    assert fs.stat("/f").size == CHUNK + 500
    assert fs._count_chunks(fs.stat("/f").inode) == 2


def test_truncate_grow_zero_pads(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"abc", offset=0)
    fs.truncate_fd(fd, 10)

    assert fs.stat("/f").size == 10
    assert fs.read(fd, size=10, offset=0) == b"abc" + b"\x00" * 7


def test_empty_file_has_zero_chunks(fs_with_file):
    fs, fd = fs_with_file
    assert fs.stat("/f").size == 0
    assert fs._count_chunks(fs.stat("/f").inode) == 0
    assert fs.read(fd, size=100, offset=0) == b""


def test_overwrite_middle_keeps_chunk_count(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"x" * 1000, offset=0)
    chunks_before = fs._count_chunks(fs.stat("/f").inode)

    fs.write(fd, b"yy", offset=100)
    chunks_after = fs._count_chunks(fs.stat("/f").inode)

    assert chunks_before == chunks_after
    data = fs.read(fd, size=1000, offset=0)
    assert data[:100] == b"x" * 100
    assert data[100:102] == b"yy"
    assert data[102:] == b"x" * 898


def test_exact_chunk_boundary_write(fs_with_file):
    fs, fd = fs_with_file
    fs.write(fd, b"a" * CHUNK, offset=0)
    assert fs.stat("/f").size == CHUNK
    assert fs._count_chunks(fs.stat("/f").inode) == 1

    fs.write(fd, b"b", offset=CHUNK)
    assert fs._count_chunks(fs.stat("/f").inode) == 2
    assert fs.stat("/f").size == CHUNK + 1


def test_large_file_multi_chunk(fs_with_file):
    fs, fd = fs_with_file
    payload = bytes((i & 0xFF) for i in range(CHUNK * 10))
    fs.write(fd, payload, offset=0)

    assert fs.stat("/f").size == CHUNK * 10
    assert fs._count_chunks(fs.stat("/f").inode) == 10

    assert fs.read(fd, size=CHUNK * 10, offset=0) == payload


def test_content_survives_reopen(tmp_db):
    from sqlite_fs import mkfs, open_fs

    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fd = fs.create("/f")
        fs.write(fd, b"persisted", offset=0)
        fs.close_fd(fd)
    fs.close()

    fs = open_fs(str(tmp_db))
    with fs.as_user(0, 0):
        fd = fs.open("/f", flags=0)
        assert fs.read(fd, size=9, offset=0) == b"persisted"
        fs.close_fd(fd)
    fs.close()
