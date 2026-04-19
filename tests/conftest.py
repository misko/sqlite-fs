import pytest

from sqlite_fs import (
    mkfs,
    open_fs,
    Filesystem,
    FilesystemError,
    PathSyntaxError,
    NotFound,
    AlreadyExists,
    NotADirectory,
    IsADirectory,
    DirectoryNotEmpty,
    PermissionDenied,
    ReadOnlyFilesystem,
    NameTooLong,
    InvalidXattr,
    LockConflict,
    BadFileDescriptor,
    SymlinkLoop,
)


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "fs.db"


@pytest.fixture
def fresh_fs(tmp_db):
    mkfs(str(tmp_db))
    fs = open_fs(str(tmp_db))
    try:
        yield fs
    finally:
        fs.close()


@pytest.fixture
def populated_fs(fresh_fs):
    with fresh_fs.as_user(0, 0):
        fresh_fs.mkdir("/notes")
        fresh_fs.mkdir("/code")
        fresh_fs.mkdir("/results")

        fd = fresh_fs.create("/notes/hello.md", mode=0o644)
        fresh_fs.write(fd, b"hello world\n", offset=0)
        fresh_fs.close_fd(fd)

        fd = fresh_fs.create("/code/hello.py", mode=0o644)
        fresh_fs.write(fd, b"print('hi')\n", offset=0)
        fresh_fs.close_fd(fd)

    yield fresh_fs


@pytest.fixture
def as_root(fresh_fs):
    with fresh_fs.as_user(0, 0):
        yield fresh_fs
