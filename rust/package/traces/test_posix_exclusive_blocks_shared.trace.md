<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_locks.py.engspec -->
<!-- test_function: test_posix_exclusive_blocks_shared -->
<!-- impl_specs: package/specs/src/locks.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:05:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-locks-excl-block -->

## Subject

```python
lm = LockManager()
lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)
with pytest.raises(LockConflict):
    lm.posix_lock(inode=1, fd_id=20, pid=2000, op="shared", start=0, length=0)
```

## Given
- No fixtures. `LockManager` instantiated inline in the test.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `lm` | Assertion line 1 | Fresh `LockManager` — no inode records |
| `record_excl` | Frame 1 Step 1.3 | Internal record: `kind="posix", type="exclusive", owner=1000, start=0, length=0` |
| `raised` | Frame 2 Step 2.4 | `LockConflict` — raised on the second call |

## Trace

### Frame 1: lm.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0)

**Call** — `LockManager.posix_lock(inode=1, fd_id=10, pid=1000, op="exclusive", start=0, length=0, wait=False)`
**Cite** — `package/specs/src/locks.py.engspec § LockManager.posix_lock`

**Step 1.1 — precondition check**

| # | Bullet | Check | ✓ |
|---|---|---|---|
| 1 | `start >= 0` | 0 ≥ 0 | ✓ |
| 2 | `length >= 0` | 0 ≥ 0 | ✓ |
| 3 | `op in {"shared","exclusive","unlock"}` | `"exclusive"` ∈ set | ✓ |

**Step 1.2 — branch selection**

Per Postconditions: `op != "unlock"` → take the acquire branch.

**Step 1.3 — conflict scan**

No existing `posix` records for inode=1 (fresh `LockManager`). No conflict.

Insert a new record: `_Record(kind="posix", type="exclusive", owner=1000, start=0, length=0)`.

Bind `record_excl = _Record(kind="posix", type="exclusive", owner=1000, start=0, length=0)`.

### Frame 2: lm.posix_lock(inode=1, fd_id=20, pid=2000, op="shared", start=0, length=0)

**Call** — `LockManager.posix_lock(inode=1, fd_id=20, pid=2000, op="shared", start=0, length=0, wait=False)`
**Cite** — `package/specs/src/locks.py.engspec § LockManager.posix_lock`

**Step 2.1 — precondition check** — all ✓

**Step 2.2 — branch selection** — `op != "unlock"` → acquire branch.

**Step 2.3 — conflict scan**

Per Postconditions: "Scan records with `kind='posix'` and `owner != pid` for conflict."
- `record_excl` has `kind="posix"`, `owner=1000`, and `pid` arg is `2000` → `owner != pid` ✓ (candidate).
- Per Postcondition definition of conflict: "overlapping range AND (either side exclusive)."
  - Range overlap: `record_excl` covers `[0, ∞)` (length=0 = to EOF). Request covers `[0, ∞)`. Overlap ✓.
  - Either side exclusive: `record_excl.type = "exclusive"` ✓.
- Conflict confirmed.

**Step 2.4 — `wait=False` branch**

Per Postconditions: "If a conflict exists: `wait=False`: raise `LockConflict`."

Raises `LockConflict`.

Bind `raised = LockConflict`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|------|-----------|-------------|-----------|
| LHS (first call) | `lm.posix_lock(..., op="exclusive", ...)` | Returns None | Frame 1 — no conflict, no raise |
| RHS | (no assertion) | — | — |
| LHS (second call) | `lm.posix_lock(..., op="shared", ...)` | Raises `LockConflict` | Frame 2 Step 2.4 |
| RHS | `LockConflict` | `LockConflict` | literal in `pytest.raises` |
| Op | `isinstance` | True | exact class match |

## Verdict: PASS
- Conflict semantics derived from two explicit Postcondition clauses: (a) the scan rule (`owner != pid`, overlap, exclusive-on-either-side), (b) the `wait=False` raise rule.
- The POSIX pid-scoping distinction (owner=1000 vs pid=2000) is what makes this conflict detectable at all — a same-pid request would not conflict.

## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:22:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Checksum: placeholder (smoke test).
- Staleness: ✓.
- Structural well-formedness: ✓.
- Citation validity: all resolve — `LockManager.posix_lock § Preconditions`, `§ Postconditions`, plus the overlap clause in Implementation Notes.
- State consistency: 3/3 references bound before use (`lm`, `record_excl`, `raised`).
- Verdict consistency: ✓ — one `isinstance` True row → PASS.

### Issues
- none

### Result
- **TRACE_VALID**. The POSIX namespace-scoping of the conflict is implicit in the spec's phrasing ("records with `kind='posix'`"). Since there's only one lock kind exercised here, the scoping is not stressed; the cross-namespace trace (`test_posix_and_flock_are_separate_namespaces`) stresses it directly.
