SCHEMA_VERSION = 2  # plan.v3: split entries from nodes.
DEFAULT_CHUNK_SIZE = 65536
ROOT_INODE = 1
MAXSYMLINKS = 40


DDL = """
CREATE TABLE schema_version (
    version INTEGER NOT NULL PRIMARY KEY,
    chunk_size INTEGER NOT NULL
);

CREATE TABLE nodes (
    inode INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'symlink')),
    mode INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    atime_ns INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    nlink INTEGER NOT NULL
);

CREATE TABLE entries (
    parent INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    name TEXT NOT NULL,
    inode INTEGER NOT NULL REFERENCES nodes(inode),
    PRIMARY KEY (parent, name)
);

CREATE INDEX entries_inode_idx ON entries (inode);

CREATE TABLE blobs (
    inode INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    chunk_id INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (inode, chunk_id)
);

CREATE TABLE xattrs (
    inode INTEGER NOT NULL REFERENCES nodes(inode) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY (inode, name)
);

CREATE TABLE symlinks (
    inode INTEGER PRIMARY KEY REFERENCES nodes(inode) ON DELETE CASCADE,
    target BLOB NOT NULL
);
"""


SYNC_LEVELS = {
    "full": 2,     # default — fsync on every commit; no data loss on power loss
    "normal": 1,   # WAL-safe — DB stays consistent; last txn may be lost on power loss
    "off": 0,      # DANGEROUS — only for unit tests / scratch workloads
}


def apply_pragmas(conn, sync_mode="full"):
    """Apply PRAGMAs. `sync_mode` overrides synchronous level per idea.md
    durability contract — default 'full' preserves the contract; callers
    who want throughput at the cost of last-transaction-on-power-loss may
    opt into 'normal'."""
    if sync_mode not in SYNC_LEVELS:
        raise ValueError(
            f"sync_mode must be one of {sorted(SYNC_LEVELS)}, got {sync_mode!r}"
        )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA synchronous = {SYNC_LEVELS[sync_mode]}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.commit()


def install_schema(conn, chunk_size):
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO schema_version (version, chunk_size) VALUES (?, ?)",
        (SCHEMA_VERSION, chunk_size),
    )
    conn.commit()


def load_chunk_size(conn):
    row = conn.execute("SELECT chunk_size FROM schema_version").fetchone()
    return row[0]
