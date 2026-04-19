use crate::errors::{Error, Result};

pub const PATH_MAX: usize = 4096;
pub const NAME_MAX: usize = 255;

pub fn parse_path(path: &str) -> Result<Vec<String>> {
    if path.is_empty() {
        return Err(Error::PathSyntax("path is empty".into()));
    }
    if path.len() > PATH_MAX {
        return Err(Error::PathSyntax(format!(
            "path exceeds PATH_MAX ({PATH_MAX})"
        )));
    }
    if !path.starts_with('/') {
        return Err(Error::PathSyntax(format!(
            "path must be absolute, got {path:?}"
        )));
    }
    if path == "/" {
        return Ok(Vec::new());
    }

    let trimmed = &path[1..];
    let trimmed = trimmed.strip_suffix('/').unwrap_or(trimmed);

    let mut components = Vec::new();
    for part in trimmed.split('/') {
        if part.is_empty() {
            return Err(Error::PathSyntax(format!(
                "empty component in path {path:?}"
            )));
        }
        if part == "." || part == ".." {
            return Err(Error::PathSyntax(format!(
                "'.' and '..' are not permitted: {path:?}"
            )));
        }
        if part.contains('\0') {
            return Err(Error::PathSyntax(format!(
                "embedded NUL in path {path:?}"
            )));
        }
        if part.len() > NAME_MAX {
            return Err(Error::NameTooLong(format!(
                "component {part:?} exceeds NAME_MAX ({NAME_MAX})"
            )));
        }
        components.push(part.to_string());
    }
    Ok(components)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_root_returns_empty() {
        assert_eq!(parse_path("/").unwrap(), Vec::<String>::new());
    }

    #[test]
    fn parse_single() {
        assert_eq!(parse_path("/foo").unwrap(), vec!["foo".to_string()]);
    }

    #[test]
    fn parse_multiple() {
        assert_eq!(
            parse_path("/a/b/c").unwrap(),
            vec!["a".to_string(), "b".to_string(), "c".to_string()]
        );
    }

    #[test]
    fn parse_unicode() {
        assert_eq!(parse_path("/café").unwrap(), vec!["café".to_string()]);
        assert_eq!(parse_path("/日本語").unwrap(), vec!["日本語".to_string()]);
    }

    #[test]
    fn rejects_empty() {
        assert!(parse_path("").is_err());
    }

    #[test]
    fn rejects_relative() {
        assert!(parse_path("foo").is_err());
        assert!(parse_path("./foo").is_err());
        assert!(parse_path("../foo").is_err());
    }

    #[test]
    fn rejects_dot_components() {
        assert!(parse_path("/a/./b").is_err());
        assert!(parse_path("/a/../b").is_err());
        assert!(parse_path("/.").is_err());
        assert!(parse_path("/..").is_err());
    }

    #[test]
    fn rejects_double_slash() {
        assert!(parse_path("//").is_err());
        assert!(parse_path("/a//b").is_err());
    }

    #[test]
    fn trailing_slash_normalized() {
        assert_eq!(parse_path("/foo/").unwrap(), vec!["foo".to_string()]);
        assert_eq!(
            parse_path("/a/b/").unwrap(),
            vec!["a".to_string(), "b".to_string()]
        );
    }

    #[test]
    fn rejects_embedded_nul() {
        assert!(parse_path("/foo\0bar").is_err());
    }

    #[test]
    fn name_too_long_boundary() {
        let ok = "a".repeat(255);
        assert_eq!(
            parse_path(&format!("/{ok}")).unwrap(),
            vec![ok.clone()]
        );
        let too_long = "a".repeat(256);
        assert!(matches!(
            parse_path(&format!("/{too_long}")),
            Err(Error::NameTooLong(_))
        ));
    }

    #[test]
    fn name_byte_length_not_char() {
        let short = "é".repeat(127); // 254 bytes, OK
        assert!(parse_path(&format!("/{short}")).is_ok());
        let too_long = "é".repeat(128); // 256 bytes, NameTooLong
        assert!(matches!(
            parse_path(&format!("/{too_long}")),
            Err(Error::NameTooLong(_))
        ));
    }

    #[test]
    fn path_too_long() {
        let comps = vec!["a"; 2048];
        let p = format!("/{}", comps.join("/"));
        assert!(parse_path(&p).is_ok());
        let comps = vec!["a"; 2049];
        let p = format!("/{}", comps.join("/"));
        assert!(matches!(parse_path(&p), Err(Error::PathSyntax(_))));
    }
}
