use std::collections::HashMap;

use rusqlite::{Connection, params};

use crate::errors::Result;

pub fn read_range(
    conn: &Connection, inode: u64, offset: u64, size: u64,
    file_size: u64, chunk_size: u64,
) -> Result<Vec<u8>> {
    if offset >= file_size || size == 0 {
        return Ok(Vec::new());
    }
    let end = (offset + size).min(file_size);
    let actual = (end - offset) as usize;
    let first = offset / chunk_size;
    let last  = (end - 1) / chunk_size;

    let mut stmt = conn.prepare(
        "SELECT chunk_id, data FROM blobs
         WHERE inode = ?1 AND chunk_id BETWEEN ?2 AND ?3
         ORDER BY chunk_id",
    )?;
    let mut present: HashMap<u64, Vec<u8>> = HashMap::new();
    let rows = stmt.query_map(
        params![inode as i64, first as i64, last as i64],
        |r| Ok((r.get::<_, i64>(0)? as u64, r.get::<_, Vec<u8>>(1)?)),
    )?;
    for row in rows {
        let (cid, data) = row?;
        present.insert(cid, data);
    }

    let mut out = Vec::with_capacity(actual);
    for cid in first..=last {
        let chunk_start = cid * chunk_size;
        let lo = offset.saturating_sub(chunk_start).min(chunk_size) as usize;
        let hi = (end - chunk_start).min(chunk_size) as usize;
        let want = hi - lo;
        match present.get(&cid) {
            Some(data) => {
                let slice_lo = lo.min(data.len());
                let slice_hi = hi.min(data.len());
                out.extend_from_slice(&data[slice_lo..slice_hi]);
                let pad = want - (slice_hi - slice_lo);
                if pad > 0 { out.extend(std::iter::repeat(0u8).take(pad)); }
            }
            None => out.extend(std::iter::repeat(0u8).take(want)),
        }
    }
    debug_assert_eq!(out.len(), actual, "chunk math bug");
    Ok(out)
}

pub fn write_range(
    conn: &Connection, inode: u64, data: &[u8], offset: u64,
    file_size: u64, chunk_size: u64,
) -> Result<u64> {
    if data.is_empty() { return Ok(file_size); }

    let n = data.len() as u64;
    let first = offset / chunk_size;
    let last  = (offset + n - 1) / chunk_size;

    let mut existing: HashMap<u64, Vec<u8>> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT chunk_id, data FROM blobs
             WHERE inode = ?1 AND chunk_id BETWEEN ?2 AND ?3",
        )?;
        for row in stmt.query_map(
            params![inode as i64, first as i64, last as i64],
            |r| Ok((r.get::<_, i64>(0)? as u64, r.get::<_, Vec<u8>>(1)?)),
        )? {
            let (cid, d) = row?;
            existing.insert(cid, d);
        }
    }

    let mut upsert = conn.prepare(
        "INSERT OR REPLACE INTO blobs (inode, chunk_id, data) VALUES (?1, ?2, ?3)",
    )?;

    let mut cursor: usize = 0;
    for cid in first..=last {
        let chunk_start = cid * chunk_size;
        let lo = offset.saturating_sub(chunk_start).min(chunk_size) as usize;
        let hi = ((offset + n) - chunk_start).min(chunk_size) as usize;
        let slice_len = hi - lo;

        let mut old = existing.remove(&cid).unwrap_or_default();
        if old.len() < lo { old.resize(lo, 0); }

        let tail_start = hi.min(old.len());
        let tail: Vec<u8> = old[tail_start..].to_vec();
        old.truncate(lo);
        old.extend_from_slice(&data[cursor..cursor + slice_len]);
        old.extend_from_slice(&tail);
        cursor += slice_len;

        upsert.execute(params![inode as i64, cid as i64, old])?;
    }

    Ok(file_size.max(offset + n))
}

pub fn truncate_to(
    conn: &Connection, inode: u64, new_size: u64, old_size: u64, chunk_size: u64,
) -> Result<()> {
    if new_size >= old_size { return Ok(()); }

    let boundary = new_size / chunk_size;
    let boundary_off = (new_size % chunk_size) as usize;

    conn.execute(
        "DELETE FROM blobs WHERE inode = ?1 AND chunk_id > ?2",
        params![inode as i64, boundary as i64],
    )?;

    if boundary_off == 0 {
        conn.execute(
            "DELETE FROM blobs WHERE inode = ?1 AND chunk_id = ?2",
            params![inode as i64, boundary as i64],
        )?;
    } else {
        let data: Option<Vec<u8>> = conn.query_row(
            "SELECT data FROM blobs WHERE inode = ?1 AND chunk_id = ?2",
            params![inode as i64, boundary as i64],
            |r| r.get(0),
        ).ok();
        if let Some(mut d) = data {
            d.truncate(boundary_off);
            conn.execute(
                "UPDATE blobs SET data = ?1 WHERE inode = ?2 AND chunk_id = ?3",
                params![d, inode as i64, boundary as i64],
            )?;
        }
    }
    Ok(())
}

pub fn count_chunks(conn: &Connection, inode: u64) -> Result<u64> {
    let n: i64 = conn.query_row(
        "SELECT COUNT(*) FROM blobs WHERE inode = ?1",
        params![inode as i64],
        |r| r.get(0),
    )?;
    Ok(n as u64)
}

pub fn total_bytes(conn: &Connection, inode: u64) -> Result<u64> {
    let n: i64 = conn.query_row(
        "SELECT COALESCE(SUM(length(data)), 0) FROM blobs WHERE inode = ?1",
        params![inode as i64],
        |r| r.get(0),
    )?;
    Ok(n as u64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nodes;
    use crate::schema::{apply_pragmas, install_schema, DEFAULT_CHUNK_SIZE, SyncMode};
    use crate::types::NodeKind;

    fn fresh_file() -> (Connection, u64) {
        let conn = Connection::open_in_memory().unwrap();
        apply_pragmas(&conn, SyncMode::Normal).unwrap();
        install_schema(&conn, DEFAULT_CHUNK_SIZE).unwrap();
        let inode = nodes::insert(&conn, NodeKind::File, 0o644, 0, 0, 1).unwrap();
        (conn, inode)
    }

    #[test]
    fn empty_read() {
        let (conn, ino) = fresh_file();
        assert!(read_range(&conn, ino, 0, 10, 0, 65536).unwrap().is_empty());
    }

    #[test]
    fn write_read_roundtrip_within_chunk() {
        let (conn, ino) = fresh_file();
        let sz = write_range(&conn, ino, b"hello", 0, 0, 65536).unwrap();
        assert_eq!(sz, 5);
        assert_eq!(read_range(&conn, ino, 0, 5, sz, 65536).unwrap(), b"hello");
    }

    #[test]
    fn write_across_chunk_boundary() {
        let (conn, ino) = fresh_file();
        let data = vec![0xabu8; 100];
        let sz = write_range(&conn, ino, &data, 60, 0, 64).unwrap();
        assert_eq!(sz, 160);
        assert_eq!(count_chunks(&conn, ino).unwrap(), 3);
        assert_eq!(read_range(&conn, ino, 60, 100, sz, 64).unwrap(), data);
    }

    #[test]
    fn write_past_eof_zero_pads_gap() {
        let (conn, ino) = fresh_file();
        let sz = write_range(&conn, ino, b"A", 0, 0, 64).unwrap();
        let sz = write_range(&conn, ino, b"B", 10, sz, 64).unwrap();
        assert_eq!(sz, 11);
        let got = read_range(&conn, ino, 0, 11, sz, 64).unwrap();
        assert_eq!(got[0], b'A');
        assert_eq!(got[10], b'B');
        assert_eq!(&got[1..10], &[0u8; 9]);
    }

    #[test]
    fn truncate_shrinks_and_grows() {
        let (conn, ino) = fresh_file();
        let data = vec![1u8; 200];
        let sz = write_range(&conn, ino, &data, 0, 0, 64).unwrap();
        truncate_to(&conn, ino, 100, sz, 64).unwrap();
        let got = read_range(&conn, ino, 0, 100, 100, 64).unwrap();
        assert_eq!(got.len(), 100);
        assert!(got.iter().all(|&b| b == 1));
        assert!(count_chunks(&conn, ino).unwrap() <= 2);
    }

    #[test]
    fn truncate_to_zero_removes_all_chunks() {
        let (conn, ino) = fresh_file();
        let data = vec![9u8; 300];
        let sz = write_range(&conn, ino, &data, 0, 0, 64).unwrap();
        truncate_to(&conn, ino, 0, sz, 64).unwrap();
        assert_eq!(count_chunks(&conn, ino).unwrap(), 0);
    }
}
