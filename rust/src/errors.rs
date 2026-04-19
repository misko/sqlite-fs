use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("path syntax: {0}")]
    PathSyntax(String),
    #[error("name too long: {0}")]
    NameTooLong(String),
    #[error("not found: {0}")]
    NotFound(String),
    #[error("already exists: {0}")]
    AlreadyExists(String),
    #[error("not a directory: {0}")]
    NotADirectory(String),
    #[error("is a directory: {0}")]
    IsADirectory(String),
    #[error("directory not empty: {0}")]
    DirectoryNotEmpty(String),
    #[error("permission denied: {0}")]
    PermissionDenied(String),
    #[error("read-only filesystem")]
    ReadOnlyFilesystem,
    #[error("invalid xattr: {0}")]
    InvalidXattr(String),
    #[error("lock conflict: {0}")]
    LockConflict(String),
    #[error("bad file descriptor: {0}")]
    BadFileDescriptor(String),
    #[error("symlink loop: {0}")]
    SymlinkLoop(String),
    #[error("invalid argument: {0}")]
    InvalidArgument(String),
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

impl Error {
    pub fn errno(&self) -> i32 {
        use libc::*;
        match self {
            Error::PathSyntax(_)        => EINVAL,
            Error::InvalidArgument(_)   => EINVAL,
            Error::InvalidXattr(_)      => EINVAL,
            Error::NameTooLong(_)       => ENAMETOOLONG,
            Error::NotFound(_)          => ENOENT,
            Error::AlreadyExists(_)     => EEXIST,
            Error::NotADirectory(_)     => ENOTDIR,
            Error::IsADirectory(_)      => EISDIR,
            Error::DirectoryNotEmpty(_) => ENOTEMPTY,
            Error::PermissionDenied(_)  => EACCES,
            Error::ReadOnlyFilesystem   => EROFS,
            Error::LockConflict(_)      => EAGAIN,
            Error::BadFileDescriptor(_) => EBADF,
            Error::SymlinkLoop(_)       => ELOOP,
            Error::Sqlite(_)            => EIO,
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;

#[cfg(test)]
mod tests {
    use super::*;
    use libc::*;

    #[test]
    fn errno_covers_every_variant() {
        let cases = [
            (Error::PathSyntax("x".into()),     EINVAL),
            (Error::NotFound("x".into()),       ENOENT),
            (Error::AlreadyExists("x".into()),  EEXIST),
            (Error::NotADirectory("x".into()),  ENOTDIR),
            (Error::IsADirectory("x".into()),   EISDIR),
            (Error::DirectoryNotEmpty("x".into()), ENOTEMPTY),
            (Error::PermissionDenied("x".into()),  EACCES),
            (Error::ReadOnlyFilesystem,         EROFS),
            (Error::NameTooLong("x".into()),    ENAMETOOLONG),
            (Error::InvalidXattr("x".into()),   EINVAL),
            (Error::LockConflict("x".into()),   EAGAIN),
            (Error::BadFileDescriptor("x".into()), EBADF),
            (Error::SymlinkLoop("x".into()),    ELOOP),
            (Error::InvalidArgument("x".into()), EINVAL),
        ];
        for (err, expected) in cases {
            assert_eq!(err.errno(), expected, "{err:?}");
        }
    }

    #[test]
    fn sqlite_error_autoconverts_via_from() {
        fn inner() -> Result<()> {
            Err(rusqlite::Error::ExecuteReturnedResults)?;
            Ok(())
        }
        let e = inner().unwrap_err();
        assert!(matches!(e, Error::Sqlite(_)));
        assert_eq!(e.errno(), libc::EIO);
    }
}
