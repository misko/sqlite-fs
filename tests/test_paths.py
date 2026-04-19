import pytest

from sqlite_fs.paths import parse_path, PATH_MAX, NAME_MAX
from sqlite_fs.errors import PathSyntaxError, NameTooLong


def test_parse_root_returns_empty_components():
    assert parse_path("/") == []


def test_parse_single_component():
    assert parse_path("/foo") == ["foo"]


def test_parse_multiple_components():
    assert parse_path("/a/b/c") == ["a", "b", "c"]


def test_parse_unicode_component():
    assert parse_path("/café") == ["café"]
    assert parse_path("/日本語") == ["日本語"]


def test_parse_rejects_empty_string():
    with pytest.raises(PathSyntaxError):
        parse_path("")


def test_parse_rejects_relative():
    with pytest.raises(PathSyntaxError):
        parse_path("foo")
    with pytest.raises(PathSyntaxError):
        parse_path("./foo")
    with pytest.raises(PathSyntaxError):
        parse_path("../foo")


def test_parse_rejects_dot_components():
    with pytest.raises(PathSyntaxError):
        parse_path("/a/./b")
    with pytest.raises(PathSyntaxError):
        parse_path("/a/../b")
    with pytest.raises(PathSyntaxError):
        parse_path("/.")
    with pytest.raises(PathSyntaxError):
        parse_path("/..")


def test_parse_rejects_empty_components():
    with pytest.raises(PathSyntaxError):
        parse_path("//")
    with pytest.raises(PathSyntaxError):
        parse_path("/a//b")


def test_parse_trailing_slash_normalized():
    assert parse_path("/foo/") == ["foo"]
    assert parse_path("/a/b/") == ["a", "b"]


def test_parse_rejects_embedded_nul():
    with pytest.raises(PathSyntaxError):
        parse_path("/foo\x00bar")


def test_parse_rejects_non_utf8():
    with pytest.raises(PathSyntaxError):
        parse_path(b"/foo")  # type: ignore[arg-type]


def test_parse_rejects_name_too_long():
    # Exactly 255 bytes (ASCII).
    ok = "a" * 255
    assert parse_path(f"/{ok}") == [ok]

    # 256 bytes: fails.
    too_long = "a" * 256
    with pytest.raises(NameTooLong):
        parse_path(f"/{too_long}")


def test_parse_name_byte_length_vs_char_length():
    # "é" is 2 bytes in UTF-8. 127 copies = 254 bytes: OK.
    short = "é" * 127
    assert parse_path(f"/{short}") == [short]

    # 128 copies = 256 bytes: NameTooLong.
    too_long = "é" * 128
    with pytest.raises(NameTooLong):
        parse_path(f"/{too_long}")


def test_parse_rejects_path_too_long():
    components = ["a"] * 2048
    path = "/" + "/".join(components)
    assert parse_path(path) == components

    components = ["a"] * 2049
    path = "/" + "/".join(components)
    with pytest.raises(PathSyntaxError):
        parse_path(path)


def test_parse_constants():
    assert PATH_MAX == 4096
    assert NAME_MAX == 255
