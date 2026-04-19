<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_paths.py.engspec -->
<!-- test_function: test_parse_root_returns_empty_components -->
<!-- impl_specs: package/specs/src/paths.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:00:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-paths-root -->

## Subject

```python
assert parse_path("/") == []
```

## Given
- No fixtures — pure function test.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `result` | Frame 1 Step 1.3 | `[]` — empty list, the root case |

## Trace

### Frame 1: parse_path("/")

**Call** — `parse_path(path="/")`
**Cite** — `package/specs/src/paths.py.engspec § parse_path`

**Step 1.1 — precondition check**

| # | Bullet | Check | ✓ |
|---|---|---|---|
| 1 | "path is a str" | `"/"` is a `str` | ✓ |

**Step 1.2 — input guards** (part of Implementation Notes)

Cite `paths.engspec § parse_path § Implementation Notes`:
- `isinstance(path, str)` → True ✓
- `path == ""` check → `"/"` is not empty ✓
- `len(path.encode("utf-8")) > PATH_MAX` check → 1 ≤ 4096 ✓
- `path.startswith("/")` check → True ✓

**Step 1.3 — branch selection**

Cite `paths.engspec § parse_path § Postconditions`:
- Postcondition bullet 1: "If `path == "/"`: returns `[]`."

Input is exactly `"/"` → take this branch. No further processing.

Bind `result = []`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|------|-----------|-------------|-----------|
| LHS | `parse_path("/")` | `[]` | Frame 1 Step 1.3 |
| RHS | `[]` | `[]` | literal in test |
| Op | `==` | True | list equality on two empty lists |

## Verdict: PASS
- Single-branch derivation; no underdetermined steps.
- The root special-case is explicitly required by Postcondition bullet 1.

## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:20:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Checksum: placeholder (smoke test).
- Staleness: ✓ — spec unchanged since trace.
- Structural well-formedness: ✓ — all sections present.
- Citation validity: 3/3 resolved (`parse_path` section, Implementation Notes, Postconditions bullet 1).
- State consistency: 1/1 (`result` bound Frame 1 Step 1.3; used only in Assertion LHS).
- Verdict consistency: ✓ — single `==` True row → PASS.

### Issues
- none

### Result
- **TRACE_VALID**. The root short-circuit case is explicitly on the critical path of the spec and is the first branch executed.
