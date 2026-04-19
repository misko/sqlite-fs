<!-- engspec-trace v1 -->
<!-- test_spec: package/specs/tests/test_paths.py.engspec -->
<!-- test_function: test_parse_rejects_dot_components -->
<!-- impl_specs: package/specs/src/paths.py.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:01:00Z -->
<!-- verdict: PASS -->
<!-- checksum: placeholder-trace-paths-dot -->

## Subject

```python
with pytest.raises(PathSyntaxError):
    parse_path("/a/./b")
with pytest.raises(PathSyntaxError):
    parse_path("/a/../b")
with pytest.raises(PathSyntaxError):
    parse_path("/.")
with pytest.raises(PathSyntaxError):
    parse_path("/..")
```

## Given
- No fixtures.

## State

| Name | Bound in | Value / description |
|---|---|---|
| `raised_dot_dot_dot` | Frame 1 | `PathSyntaxError` — raised on `.` component |
| `raised_dotdot` | Frame 2 | `PathSyntaxError` — raised on `..` component |
| `raised_only_dot` | Frame 3 | `PathSyntaxError` — `/.` single-dot component |
| `raised_only_dotdot` | Frame 4 | `PathSyntaxError` — `/..` single-dotdot component |

## Trace

### Frame 1: parse_path("/a/./b")

**Call** — `parse_path(path="/a/./b")`
**Cite** — `package/specs/src/paths.py.engspec § parse_path`

**Step 1.1 — precondition check**

| # | Bullet | Check | ✓ |
|---|---|---|---|
| 1 | "path is a str" | `"/a/./b"` is a `str` | ✓ |

**Step 1.2 — guards pass**

- Not empty, length OK, starts with `/` — all pass per Implementation Notes.
- path != "/" so the root short-circuit does not apply.

**Step 1.3 — split and validate components**

Cite `paths.engspec § parse_path § Postconditions`:
- "Consume leading `/`, optional trailing `/`, split on `/`."
- Trimmed input: `"a/./b"`. Components: `["a", ".", "b"]`.

Cite `paths.engspec § parse_path § Failure Modes`:
- "Any component is `'.'` or `'..'`: raises `PathSyntaxError`."

The second component is `"."` → exact match for this Failure Mode.

**Step 1.4 — raise**

Raises `PathSyntaxError("'.' and '..' are not permitted: '/a/./b'")`.

Bind `raised_dot_dot_dot = PathSyntaxError`.

### Frame 2: parse_path("/a/../b")

Same structure. Components: `["a", "..", "b"]`. Second component is `".."` → same Failure Mode → raises `PathSyntaxError`.

Bind `raised_dotdot = PathSyntaxError`.

### Frame 3: parse_path("/.")

Trimmed input: `"."`. Components: `["."]`. Failure Mode fires on the sole component.

Bind `raised_only_dot = PathSyntaxError`.

### Frame 4: parse_path("/..")

Trimmed input: `".."`. Components: `[".."]`. Failure Mode fires.

Bind `raised_only_dotdot = PathSyntaxError`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|------|-----------|-------------|-----------|
| LHS | `parse_path("/a/./b")` | Raises `PathSyntaxError` | Frame 1 Step 1.4 |
| RHS | `PathSyntaxError` | `PathSyntaxError` | literal in `pytest.raises` |
| Op | `isinstance` | True | |
| LHS | `parse_path("/a/../b")` | Raises `PathSyntaxError` | Frame 2 |
| RHS | `PathSyntaxError` | | |
| Op | `isinstance` | True | |
| LHS | `parse_path("/.")` | Raises `PathSyntaxError` | Frame 3 |
| RHS | `PathSyntaxError` | | |
| Op | `isinstance` | True | |
| LHS | `parse_path("/..")` | Raises `PathSyntaxError` | Frame 4 |
| RHS | `PathSyntaxError` | | |
| Op | `isinstance` | True | |

## Verdict: PASS
- All four assertions derive identical Failure-Mode citations.
- No underdetermined steps.
- The spec explicitly names `.`/`..` as rejected under "Any component is `'.'` or `'..'`", so the rejection is not a question of "how should it behave" — it's a question of "does the impl walk its components and check this rule", and the Failure Mode is literal.

## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:21:00Z -->
<!-- verified_checksum: placeholder -->
<!-- result: TRACE_VALID -->

### Checks performed
- Checksum: placeholder (smoke test).
- Staleness: ✓.
- Structural well-formedness: ✓.
- Citation validity: 4/4 — all cite the same two sections (`parse_path § Postconditions`, `§ Failure Modes`).
- State consistency: 4/4 — each `raised_*` bound in its corresponding Frame, used only in the Assertion table.
- Verdict consistency: ✓ — all four Op rows `isinstance`=True; implied verdict PASS.

### Issues
- none

### Result
- **TRACE_VALID**. All four assertions share a single spec citation (Failure Mode: "Any component is `'.'` or `'..'`"). The trace correctly stops at component validation rather than continuing to the full-component loop, because the first matching component short-circuits the loop.
