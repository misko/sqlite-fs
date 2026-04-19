<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_perms.py.engspec -->
<!-- test_function: test_root_execute_requires_at_least_one_x_bit -->
<!-- impl_specs: package/specs/src/perms.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:12:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-perms-rootx -->

## Subject

```python
# No execute bit at all — root denied.
assert check_access(
    node_mode=0o666, node_uid=1000, node_gid=1000,
    caller_uid=0, caller_gid=0,
    access=Access.X,
) is False

# Any execute bit set — root allowed.
assert check_access(
    node_mode=0o001, node_uid=1000, node_gid=1000,
    caller_uid=0, caller_gid=0,
    access=Access.X,
) is True
```

## Given
- No fixtures — pure predicate.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `result_no_x` | Frame 1 Step 1.3 | `False` — root denied X on mode 0o666 |
| `result_other_x` | Frame 2 Step 2.3 | `True` — root allowed X on mode 0o001 |

## Trace

### Frame 1: check_access(node_mode=0o666, ..., access=Access.X)

**Call** — `check_access(node_mode=0o666, node_uid=1000, node_gid=1000, caller_uid=0, caller_gid=0, access=Access.X)`
**Cite** — `package/specs/src/perms.py.engspec § check_access`

**Step 1.1 — precondition check**
- `node_mode` ∈ `[0, 0o7777]` — `0o666` ✓.
- `caller_uid = 0` → root path taken.

**Step 1.2 — branch selection**

Cite `perms.engspec § check_access § Postconditions`:
- Root bypass clause: "If `caller_uid == 0` (root) … For `X`: granted iff at least one execute bit is set anywhere in mode (owner/group/other)."

Input has `access = Access.X` → the X-specific conditional applies.

**Step 1.3 — apply the X condition**

Cite Implementation Notes:
```python
if Access.X in access:
    any_x = (node_mode & 0o111) != 0
    if not any_x:
        return False
```

Compute `0o666 & 0o111`:
- `0o666` in binary: `110 110 110`
- `0o111` in binary: `001 001 001`
- AND: `000 000 000` = `0o000`.

`any_x == 0 != 0` is False → enter `if not any_x` branch → return `False`.

Bind `result_no_x = False`.

### Frame 2: check_access(node_mode=0o001, ..., access=Access.X)

**Call** — same, with `node_mode=0o001`.
**Cite** — same.

**Step 2.1 — precondition check** — ✓.

**Step 2.2 — branch selection** — same X-specific conditional applies.

**Step 2.3 — apply the X condition**

Compute `0o001 & 0o111 = 0o001 ≠ 0` → `any_x = True`.

Fall through: Implementation Notes continues:
```python
    # Strip X from further checks — the rest (R/W) is granted.
    access_remaining = access & ~Access.X
else:
    access_remaining = access
if access_remaining == Access(0):
    return True
return True  # root has full R/W regardless of mode bits
```

`access = Access.X`, so `access_remaining = Access.X & ~Access.X = Access(0)`.
The next check: `access_remaining == Access(0)` → True → return `True`.

Bind `result_other_x = True`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|------|-----------|-------------|-----------|
| LHS (1st) | `check_access(0o666, ..., Access.X)` | `False` | Frame 1 Step 1.3 |
| RHS (1st) | `False` | `False` | literal |
| Op | `is` | True | both `False` literals |
| LHS (2nd) | `check_access(0o001, ..., Access.X)` | `True` | Frame 2 Step 2.3 |
| RHS (2nd) | `True` | `True` | literal |
| Op | `is` | True | both `True` literals |

## Verdict: PASS
- The "root X conditional on at-least-one-X-bit" rule is derivable from the explicit Postcondition wording and the Implementation Notes' fenced code block.
- `0o666 & 0o111 == 0` is a literal bitwise computation — the spec-trace can verify this without executing code.
- Both halves of the test exercise the two sides of the conditional; the spec is sufficient for both.

## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:24:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Checksum: placeholder (smoke test).
- Staleness: ✓.
- Structural well-formedness: ✓.
- Citation validity: ✓ — Postconditions root-X clause and Implementation Notes fenced code block both resolve.
- State consistency: 2/2 references bound.
- Verdict consistency: ✓ — both assertions `is True`; PASS matches.

### Issues
- none

### Result
- **TRACE_VALID**. The bitwise computations in both frames (`0o666 & 0o111 = 0`, `0o001 & 0o111 = 0o001`) are verifiable at the spec level — no runtime needed. This trace is a good demonstration of tracing pure-logic algorithmic specs.
