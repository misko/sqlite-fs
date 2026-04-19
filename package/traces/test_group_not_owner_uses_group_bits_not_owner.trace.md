<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_perms.py.engspec -->
<!-- test_function: test_group_not_owner_uses_group_bits_not_owner -->
<!-- impl_specs: package/specs/src/perms.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:15:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-perms-group -->

## Subject

```python
# Owner can do anything (0o700); group can do nothing (0o000).
# Caller is in the group but is not the owner.
assert check_access(
    node_mode=0o700, node_uid=1000, node_gid=2000,
    caller_uid=9999, caller_gid=2000,
    access=Access.R,
) is False
```

## Given
- No fixtures.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `result` | Frame 1 Step 1.4 | `False` — group denied because group bits are 000 |

## Trace

### Frame 1: check_access(node_mode=0o700, node_uid=1000, node_gid=2000, caller_uid=9999, caller_gid=2000, access=Access.R)

**Call** — same as Subject.
**Cite** — `package/specs/src/perms.py.engspec § check_access`

**Step 1.1 — precondition check** — all ✓.

**Step 1.2 — branch selection: not root**

Cite Postconditions:
- `caller_uid == 0`? No (9999) → skip root bypass.
- `caller_uid == node_uid`? 9999 == 1000? No → skip owner branch.
- `caller_gid == node_gid`? 2000 == 2000? Yes → take group branch.

Cite Implementation Notes: `bits = (node_mode >> 3) & 0o7`.

**Step 1.3 — compute group bits**

- `node_mode = 0o700`.
- `0o700 >> 3 = 0o070`.
- `0o070 & 0o7 = 0` → `bits = 0`.

Key derivation: the spec says "Else if `caller_gid == node_gid`: use the group bits **`(mode >> 3) & 0o7`**." The implementation selects the group triple BEFORE checking what's in it. Once selected, only the group triple matters — the owner triple `0o700 >> 6 & 0o7 = 7` is not consulted.

**Step 1.4 — check required access against group bits**

Cite Implementation Notes:
```python
required = access.value   # Flag.value is the int combo
return (bits & required) == required
```

- `access = Access.R`, so `required = 4`.
- `bits = 0`.
- `(0 & 4) == 4`? `0 == 4`? No → return `False`.

Bind `result = False`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|------|-----------|-------------|-----------|
| LHS | `check_access(0o700, 1000, 2000, 9999, 2000, Access.R)` | `False` | Frame 1 Step 1.4 |
| RHS | `False` | `False` | literal |
| Op | `is` | True | identity on `False` |

## Verdict: PASS
- The "group-but-not-owner uses group bits, not owner bits" semantic is explicitly mandated by the ordered elif chain in Postconditions + the selection rule in Implementation Notes.
- Any implementation that "also" checks owner bits (or falls through on deny) would produce `True` here, contradicting the assertion — so the trace requires the spec's strict elif behavior.

## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:25:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Checksum: placeholder (smoke test).
- Staleness: ✓.
- Structural well-formedness: ✓.
- Citation validity: ✓ — Postconditions elif chain + Implementation Notes `bits` computation.
- State consistency: 1/1.
- Verdict consistency: ✓ — `is False` matches PASS.

### Issues
- none

### Result
- **TRACE_VALID**. The elif-chain semantics is the *only* place where group-but-not-owner behavior is pinned. Removing the spec's "Else if" phrasing would make the behavior ambiguous. A common regression would be to use `if`/`if` instead of `if`/`elif` — which this test would catch at runtime, but the trace catches it at spec level.
