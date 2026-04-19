def read_range(conn, inode, offset, size, *, file_size, chunk_size):
    if offset >= file_size or size == 0:
        return b""
    end = min(offset + size, file_size)
    actual_size = end - offset

    first_chunk = offset // chunk_size
    last_chunk = (end - 1) // chunk_size

    rows = conn.execute(
        """SELECT chunk_id, data FROM blobs
           WHERE inode = ? AND chunk_id BETWEEN ? AND ?
           ORDER BY chunk_id""",
        (inode, first_chunk, last_chunk),
    ).fetchall()
    present = {cid: bytes(data) for cid, data in rows}

    parts = []
    for cid in range(first_chunk, last_chunk + 1):
        chunk_start = cid * chunk_size
        in_chunk_lo = max(0, offset - chunk_start)
        in_chunk_hi = min(chunk_size, end - chunk_start)
        chunk_data = present.get(cid, b"")
        piece = chunk_data[in_chunk_lo:in_chunk_hi]
        if len(piece) < in_chunk_hi - in_chunk_lo:
            piece = piece + b"\x00" * (in_chunk_hi - in_chunk_lo - len(piece))
        parts.append(piece)

    result = b"".join(parts)
    assert len(result) == actual_size, "chunk math bug"
    return result


def write_range(conn, inode, data, offset, *, file_size, chunk_size):
    if len(data) == 0:
        return file_size

    first_chunk = offset // chunk_size
    last_chunk = (offset + len(data) - 1) // chunk_size

    existing = {
        cid: bytes(d) for cid, d in conn.execute(
            """SELECT chunk_id, data FROM blobs
               WHERE inode = ? AND chunk_id BETWEEN ? AND ?""",
            (inode, first_chunk, last_chunk),
        ).fetchall()
    }

    to_write = []
    data_cursor = 0
    for cid in range(first_chunk, last_chunk + 1):
        chunk_start = cid * chunk_size
        in_chunk_lo = max(0, offset - chunk_start)
        in_chunk_hi = min(chunk_size, offset + len(data) - chunk_start)
        slice_len = in_chunk_hi - in_chunk_lo

        old = existing.get(cid, b"")
        if len(old) < in_chunk_lo:
            old = old + b"\x00" * (in_chunk_lo - len(old))
        new = (old[:in_chunk_lo]
               + data[data_cursor:data_cursor + slice_len]
               + old[in_chunk_hi:])
        data_cursor += slice_len
        to_write.append((cid, new))

    conn.executemany(
        "INSERT OR REPLACE INTO blobs (inode, chunk_id, data) VALUES (?, ?, ?)",
        [(inode, cid, blob) for cid, blob in to_write],
    )

    return max(file_size, offset + len(data))


def truncate_to(conn, inode, new_size, *, old_size, chunk_size):
    if new_size == old_size:
        return

    if new_size < old_size:
        boundary = new_size // chunk_size
        boundary_offset = new_size % chunk_size
        conn.execute(
            "DELETE FROM blobs WHERE inode = ? AND chunk_id > ?",
            (inode, boundary),
        )
        if boundary_offset == 0:
            conn.execute(
                "DELETE FROM blobs WHERE inode = ? AND chunk_id = ?",
                (inode, boundary),
            )
        else:
            row = conn.execute(
                "SELECT data FROM blobs WHERE inode = ? AND chunk_id = ?",
                (inode, boundary),
            ).fetchone()
            if row is not None:
                trimmed = bytes(row[0])[:boundary_offset]
                conn.execute(
                    "UPDATE blobs SET data = ? WHERE inode = ? AND chunk_id = ?",
                    (trimmed, inode, boundary),
                )
    # Growth: no-op; reads past EOF return zeros.


def count_chunks(conn, inode):
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM blobs WHERE inode = ?", (inode,),
    ).fetchone()
    return n


def total_bytes(conn, inode):
    (n,) = conn.execute(
        "SELECT COALESCE(SUM(length(data)), 0) FROM blobs WHERE inode = ?",
        (inode,),
    ).fetchone()
    return n
