from sqlite_fs.errors import NameTooLong, PathSyntaxError


PATH_MAX = 4096
NAME_MAX = 255


def parse_path(path):
    if not isinstance(path, str):
        raise PathSyntaxError(
            f"path must be str, got {type(path).__name__}"
        )

    if path == "":
        raise PathSyntaxError("path is empty")

    if len(path.encode("utf-8")) > PATH_MAX:
        raise PathSyntaxError(f"path exceeds PATH_MAX ({PATH_MAX})")

    if not path.startswith("/"):
        raise PathSyntaxError(f"path must be absolute, got {path!r}")

    if path == "/":
        return []

    # Strip exactly one trailing slash if present.
    trimmed = path[1:-1] if path.endswith("/") else path[1:]
    components = trimmed.split("/")

    for component in components:
        if component == "":
            raise PathSyntaxError(f"empty component in path {path!r}")
        if component in (".", ".."):
            raise PathSyntaxError(
                f"'.' and '..' are not permitted: {path!r}"
            )
        if "\x00" in component:
            raise PathSyntaxError(f"embedded NUL in path {path!r}")
        if len(component.encode("utf-8")) > NAME_MAX:
            raise NameTooLong(
                f"component {component!r} exceeds NAME_MAX ({NAME_MAX})"
            )

    return components
