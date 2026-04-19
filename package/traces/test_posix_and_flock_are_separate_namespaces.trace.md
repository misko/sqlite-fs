<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_locks.py.engspec -->
<!-- test_function: test_posix_and_flock_are_separate_namespaces -->
<!-- impl_specs: package/specs/src/locks.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:08:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-locks-ns -->

## Subject

```python
lm = LockManager()
lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
lm.flock(inode=1, fd_id=20, op="exclusive")
lm.ofd_lock(inode=1, fd_id=30, op="exclusive", start=0, length=0)
```

## Given
- No fixtures.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `lm` | Line 1 | Fresh `LockManager` |
| `rec_posix` | Frame 1 Step 1.3 | `_Record(kind="posix", type="exclusive", owner=1000, start=0, length=0)` |
| `rec_flock` | Frame 2 Step 2.3 | `_Record(kind="flock", type="exclusive", owner=20, start=0, length=0)` |
| `rec_ofd` | Frame 3 Step 3.3 | `_Record(kind="ofd", type="exclusive", owner=30, start=0, length=0)` |

## Trace

### Frame 1: lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)

Cite `locks.engspec § LockManager.posix_lock`. Normal acquire — no prior records. Insert `rec_posix`.

### Frame 2: lm.flock(inode=1, fd_id=20, op="exclusive")

**Cite** — `locks.engspec § LockManager.flock`

**Step 2.1 — precondition check**
- `op ∈ {"shared","exclusive","unlock"}` — `"exclusive"` ✓.

**Step 2.2 — branch selection**
- Cite Postconditions: "Records have `start=0, length=0` always" and "Conflict rules: exclusive conflicts with any other [flock]."
- Key word: "any other **flock**". Conflicts are restricted to `kind="flock"` records.
- Input is `op="exclusive"` → check for conflicting `kind="flock"` records.

**Step 2.3 — conflict scan**
- Records on inode=1: only `rec_posix` (kind="posix"). No `kind="flock"` records.
- No conflict in the flock namespace. Insert `rec_flock`.

This is the key derivation: `rec_posix` exists but has `kind="posix"`, and the conflict check in `locks.engspec § LockManager.flock § Postconditions` is scoped to `kind="flock"` records only. The POSIX record is invisible to the flock scan.

### Frame 3: lm.ofd_lock(inode=1, fd_id=30, op="exclusive", start=0, length=0)

**Cite** — `locks.engspec § LockManager.ofd_lock`

**Step 3.1 — precondition check** — all ✓.

**Step 3.2 — branch selection**
- Cite Postconditions: "Exactly `posix_lock` semantics but with `owner=fd_id` and `kind='ofd'`. Compare conflict against other `kind='ofd'` records only."
- Conflict scoped to `kind="ofd"` records only.

**Step 3.3 — conflict scan**
- Records on inode=1: `rec_posix` (kind="posix"), `rec_flock` (kind="flock"). No `kind="ofd"` records.
- No conflict. Insert `rec_ofd`.

## Assertion evaluation

| Side | Expression | Result | Derivation |
|------|-----------|--------|-----------|
| Frame 1 call | `lm.posix_lock(...)` | Returns None (no raise) | Frame 1 (no prior records) |
| Frame 2 call | `lm.flock(...)` | Returns None (no raise) | Frame 2 Step 2.3 |
| Frame 3 call | `lm.ofd_lock(...)` | Returns None (no raise) | Frame 3 Step 3.3 |

The test uses no `pytest.raises` — every call must simply return. All three return normally.

## Verdict: PASS

- The three flavors' namespace independence is derived from an explicit scoping phrase in each Postcondition: flock's "any other flock" and OFD's "kind='ofd' records only". POSIX would need the analogous scoping for its own check to match.
- **Methodology note for the verifier**: `posix_lock § Postconditions` phrases the scan as "records with `kind='posix'` and `owner != pid`". The explicit `kind="posix"` filter pins that POSIX does not see OFD or flock records as conflicts either. This test would trace UNCLEAR if any of the three Postconditions left namespace scoping implicit — but all three are explicit.

## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:23:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Checksum: placeholder (smoke test).
- Staleness: ✓.
- Structural well-formedness: ✓.
- Citation validity: 3/3 — `LockManager.posix_lock`, `§ flock`, `§ ofd_lock`. Each scoping phrase verified against the current impl engspec.
- State consistency: 4/4 references bound before use.
- Verdict consistency: ✓ — three normal returns, no raises — PASS.

### Issues
- **Minor** (non-blocking): the `posix_lock § Postconditions` text uses "records with `kind='posix'`" — the scoping phrase. However, `ofd_lock § Postconditions` refers back to "exactly `posix_lock` semantics but with `kind='ofd'`" — this is phrased as a cross-reference rather than standalone. The trace resolves correctly because the reader can substitute, but a stricter impl engspec would spell out the `kind="ofd"`-only scoping explicitly in `ofd_lock` rather than by reference. Recorded as a methodology-level finding.

### Result
- **TRACE_VALID** with one quality note. The three namespaces don't conflict because each Postcondition's conflict scan is scoped to its own `kind`. Implementation cost: three separate lists indexed by kind, or one list with a kind filter.
