# engspec-trace: skill + format design

## One-line intent

Two Claude Code skills and one format document that let any Claude evaluate
an engspec test case as **PASS / FAIL / UNCLEAR** from spec alone — no code
executed — and let a second Claude verify that evaluation in a fraction of
the cost.

## Why two skills, not one

A generator produces a trace. A verifier checks it. They're different jobs:

- **Generator** must re-derive every step from the specs. Expensive: one
  careful forward pass across all cited sections.
- **Verifier** only has to check each step is locally sound — does the
  citation exist? does the derivation follow? Cheap: proof-checking is
  strictly easier than proof-finding.

Keeping them separate means the verifier prompt can be small and fast, and
that the human-facing verdict ("this trace is valid") is produced by an
agent that hasn't already committed to an answer.

## The two skills

### `/engspec-trace <test-spec> :: <test-function> [against <impl-spec>...]`

**Inputs:**
- A path to a test `.engspec` file
- The name of a `##` section within it (the test to trace)
- Zero or more impl `.engspec` paths (auto-discovered from the test spec's
  `Context` section if omitted)

**Outputs:**
- A trace file at `<test-spec-dir>/traces/<test-function>.trace.md`
- A terminal summary: `VERDICT: PASS | FAIL | UNCLEAR — see <trace-path>`

**Behavior:** load all referenced specs, walk the test's setup + assertion
step by step against the impl specs' preconditions/postconditions/invariants,
write a structured trace, emit a verdict.

### `/engspec-verify-trace <trace-file>`

**Inputs:** a path to an existing trace file.

**Outputs:** appends a `## Verification` section to the trace file with
`TRACE_VALID | TRACE_INVALID` and a per-step check log.

**Behavior:** re-read every citation against the current specs, confirm each
step's inputs are derivable from the prior state + cited section, confirm
the verdict matches the assertion evaluation.

---

## The trace format (v1)

```markdown
<!-- engspec-trace v1 -->
<!-- test_spec: tests/posix_basic.engspec -->
<!-- test_function: test_write_read_roundtrip -->
<!-- impl_specs: nodes.engspec, blobs.engspec, fts.engspec -->
<!-- traced_by: claude-opus-4-7 -->
<!-- traced_at: 2026-04-19T14:00:00Z -->
<!-- verdict: PASS -->
<!-- checksum: <md5 of canonical trace body> -->

## Subject
<the test's assertion code block, verbatim>

## Given
- <each bullet cites test_spec § Preconditions or § Fixtures>
- fixture `fresh_fs`: yields (mount, db, daemon) with empty nodes table.
  — from tests/conftest.engspec § fresh_fs.Postconditions.
- <setup code blocks, verbatim>

## State
Named values introduced during the trace live here. The generator writes
entries as they're bound; the verifier uses this table to confirm later
references resolve to something earlier-established.

| Name | Bound in | Value / description |
|---|---|---|
| `inode_a` | Frame 1 Step 1.3 | new inode for /a.md, kind=file, size=5 |
| `blob_a` | Frame 1 Step 1.3 | blobs row (inode=inode_a, data=b"hello") |

## Trace

### Frame 1: fs.write("/a.md", b"hello")

**Call** — `nodes.write(path="/a.md", data=b"hello")`
**Cite** — `nodes.engspec § write`

**Step 1.1 — precondition check**
| # | Bullet from spec | Check against input | ✓/✗/? |
|---|---|---|---|
| 1 | "path is absolute, ≤ 4096 bytes" | "/a.md" → absolute, 5 bytes | ✓ |
| 2 | "every segment ≤ 255 bytes" | "a.md" → 4 bytes | ✓ |
| 3 | "parent directory exists" | "/" always exists post-mkfs | ✓ |
— All preconditions satisfied. Normal path.

**Step 1.2 — branch selection**
Spec has two branches per Implementation Notes: "exists → overwrite"
vs "new file → create". Target `/a.md` does not exist (from Given) →
take new-file branch.

**Step 1.3 — postcondition application**
| # | Postcondition | Resolved state |
|---|---|---|
| 1 | "a node exists at path with kind='file'" | bind `inode_a`, insert nodes row |
| 2 | "blobs row exists with data=input" | bind `blob_a`, insert blobs row |
| 3 | "fts row exists iff is_text(data)" | is_text(b"hello") = true per fts.engspec § is_text → insert fts row |
| 4 | "parent.mtime = now" | update nodes["/"].mtime |

**Step 1.4 — invariant check**
Cite `invariants.engspec § size_matches_blob`:
`nodes[inode_a].size == length(blob_a.data)` → 5 == 5 ✓.

### Frame 2: fs.read("/a.md")

**Call** — `nodes.read(path="/a.md", offset=0, size=∞)`
**Cite** — `nodes.engspec § read`

**Step 2.1 — precondition check**
| # | Bullet | Check | ✓ |
|---|---|---|---|
| 1 | "path exists" | `inode_a` from Frame 1 | ✓ |
| 2 | "node is a file" | nodes[inode_a].kind='file' | ✓ |

**Step 2.2 — postcondition application**
Per Postcondition 1: "returns blobs[inode].data[offset:offset+size]".
Resolves to: `blob_a.data[0:∞]` = `b"hello"`.

## Assertion evaluation

| Side | Expression | Resolved to | Derivation |
|---|---|---|---|
| LHS | `fs.read("/a.md")` | `b"hello"` | Frame 2 Step 2.2 |
| RHS | `b"hello"` | `b"hello"` | literal in test assertion |
| Op | `==` | True | byte-equality on equal literals |

## Verdict: PASS
- Every step derivable from cited specs.
- No underdetermined branches, no uncited facts.
- All four impl-spec postconditions in Frame 1 were used; none were ignored
  or contradicted.
```

### FAIL and UNCLEAR variants

**FAIL** — the final section changes shape:

```markdown
## Verdict: FAIL

**Root cause** — Frame 2 Step 2.2 derives `b""`, not `b"hello"`, because:
- `nodes.engspec § read § Implementation Notes` says "returns empty bytes
  if O_DIRECT flag is set and size exceeds page boundary".
- The test's setup does not specify O_DIRECT but also does not specify
  its absence; `tests/conftest.engspec § fresh_fs` defaults O_DIRECT to…
  undefined.

**Resolution options**
1. Test spec sets `O_DIRECT=False` explicitly in Preconditions.
2. Impl spec clarifies default O_DIRECT is False.

**Recommended** — option 2: fix the impl spec (callers shouldn't have to
name every optional flag).
```

**UNCLEAR** — structurally similar, but names the gap:

```markdown
## Verdict: UNCLEAR

**Underdetermined step** — Frame 1 Step 1.3 Postcondition 3: "fts row
exists iff is_text(data)". `is_text` is referenced but has no section in
`fts.engspec` — its behavior on `b"hello"` cannot be derived from the spec.

**Gap location** — `fts.engspec § is_text` (missing).

**Why it matters** — downstream `tests/search.engspec` tests depend on
this; all their traces will be UNCLEAR until `is_text` is specified.

**Suggested spec strengthening** — add to `fts.engspec`:

    ## `is_text(data: bytes) -> bool`
    ### Postconditions
    - Returns True iff data decodes as UTF-8 AND contains no null bytes
      in the first 8KB AND any of: first 1KB is >95% printable ASCII,
      filename extension in TEXT_EXTS.

After adding this, re-run `/engspec-trace` to upgrade the verdict.
```

---

## Verdict semantics — the decision rules

The generator emits exactly one of three verdicts. These rules are the
contract:

| Condition | Verdict |
|---|---|
| Every step's inputs/outputs derive from earlier steps + cited spec bullets, and the final assertion holds | **PASS** |
| Every step derives, but the final assertion does not hold — the specs actively forbid what the test expects | **FAIL** |
| At least one step cannot be derived — a cited section is missing, ambiguous, or allows multiple outputs | **UNCLEAR** |

**A PASS is not "the implementation is correct"** — it means "the spec is
sufficient for this assertion, and any correct implementation of the spec
would pass this test." That's exactly the guarantee we want before writing
code.

**A FAIL does not mean the test is wrong** — it means test and impl spec
disagree. The generator does not pick a side; the trace names both and
recommends which to change.

**An UNCLEAR trace is the most valuable output** — it identifies a
specific gap, in a specific section, with a suggested fix. It is the
primary mechanism for finding spec holes before code exists.

---

## State tracking — named values

The `## State` table is load-bearing. Every value the trace introduces
(new inode, new row, computed slice) gets a name and a binding step. Later
references must name a value already in the table.

Why: without this, a trace can "drift" — Frame 3 uses a value nobody
remembers Frame 1 introducing. With named state, the verifier checks each
reference against the table in O(n) instead of re-reading every frame.

The verifier's rule: if Frame N references a name, it must appear in the
State table with a "Bound in" step strictly before N.

---

## Citations — format and rules

Every derivation step cites a specific location. The format is:

    <spec-file> § <function-or-file-level-section>
    <spec-file> § <function> § <section-name>
    <spec-file> § <function> § <section-name> bullet <n>

Examples:
- `nodes.engspec § write`
- `nodes.engspec § write § Preconditions`
- `fts.engspec § is_text § Postconditions bullet 2`

**The verifier's job per citation**: open the cited file, find the cited
location, confirm the quoted or summarized content matches the current
spec. If the spec has changed since the trace was written (detectable via
the function-level checksum), the trace is flagged stale — not invalid,
just in need of re-generation.

---

## Canonical form + checksum

Traces get checksummed the same way engspecs do:

1. Strip `<!-- checksum: ... -->` and `<!-- verified_at: ... -->` lines
2. Strip the `## Verification` section if present
3. Normalize line endings, strip trailing whitespace, collapse blank runs
4. MD5 of UTF-8 encoded result

The checksum is what the verifier signs. If it differs from the value in
the header, something was edited post-generation — the verification
result below is stale.

---

## Generation workflow (what `/engspec-trace` does)

1. **Load inputs.** Read the test engspec and identify the target test
   function. Read its Preconditions, Postconditions, Implementation Notes.
2. **Discover impl specs.** From the test's `Context: Tests:` line, walk
   to each referenced impl spec. Optionally take explicit paths via args.
3. **Extract assertions.** Every code block in Postconditions becomes an
   assertion to evaluate. Every code block in Preconditions/Implementation
   Notes becomes a setup step.
4. **Walk frame by frame.** Each call in the setup/assertion becomes a
   frame. For each frame: precondition check → branch select → postcondition
   apply → invariant check → state update.
5. **Evaluate assertions.** For each assertion, resolve LHS via trace state,
   resolve RHS via literal, apply the comparison operator.
6. **Emit verdict.** Per the decision rules above. For UNCLEAR, name the
   exact spec section that's missing or ambiguous.
7. **Write trace file** to `<test-spec-dir>/traces/<test-function>.trace.md`.

## Verification workflow (what `/engspec-verify-trace` does)

A verifier does **not** re-derive. It checks local soundness:

1. **Checksum match.** Recompute the trace's canonical checksum; confirm
   it matches the header.
2. **Citation validity.** For each citation, open the spec and confirm the
   section exists and says what the trace quotes.
3. **State table consistency.** Every named reference in a frame must
   appear in the State table with a Bound-in step < the referring step.
4. **Step locality.** Each step's output must follow from (a) the cited
   section and (b) the State rows introduced up to that point. No outside
   facts.
5. **Verdict consistency.** The final verdict must match the assertion
   evaluation table: PASS iff all comparisons True, FAIL iff at least one
   False and no UNCLEAR steps, UNCLEAR iff at least one step is flagged.

Verifier output is appended as `## Verification`:

```markdown
## Verification
<!-- verified_by: claude-opus-4-7 -->
<!-- verified_at: 2026-04-19T14:30:00Z -->
<!-- verified_checksum: <md5 of trace body at verification> -->
<!-- result: TRACE_VALID -->

- Step-by-step: all 12 citations resolved, all 6 state references bound, verdict consistent.
- Issues: none.
```

Or, for an invalid trace:

```markdown
## Verification
<!-- result: TRACE_INVALID -->

- Issues:
  - Frame 2 Step 2.2 cites `nodes.engspec § read § Postconditions bullet 1`
    but the bullet says "returns blobs.data[offset:offset+size], zero-pad
    beyond EOF" — trace omitted the zero-pad clause.
  - Verdict PASS is still correct (input doesn't reach EOF), but the trace
    is not locally sound.
- Recommend regeneration.
```

---

## File layout

```
~/.claude/skills/engspec-trace/
├── SKILL.md                   # generator prompt
├── TRACE_FORMAT.md            # shared format spec (this doc, minus the plan prose)
├── examples/
│   ├── pass.trace.md          # worked PASS example
│   ├── fail.trace.md          # worked FAIL example
│   └── unclear.trace.md       # worked UNCLEAR example
└── README.md                  # human-facing docs

~/.claude/skills/engspec-verify-trace/
├── SKILL.md                   # verifier prompt
└── README.md                  # references TRACE_FORMAT.md in the other skill
```

Both skills reference the same `TRACE_FORMAT.md` — the verifier must agree
with the generator about what a valid trace looks like. Single source of
truth.

---

## Invocation examples

```bash
# Trace one test against auto-discovered impl specs
/engspec-trace tests/posix_basic.engspec::test_write_read_roundtrip

# Trace with explicit impl specs (when auto-discovery would miss one)
/engspec-trace tests/search.engspec::test_rename_preserves_fts \
    against nodes.engspec fts.engspec

# Batch: trace every test function in a test engspec
/engspec-trace tests/posix_basic.engspec --all

# Verify an existing trace
/engspec-verify-trace tests/posix_basic.engspec/traces/test_write_read_roundtrip.trace.md

# Verify every trace in a directory
/engspec-verify-trace tests/posix_basic.engspec/traces/ --all
```

---

## How this plugs into the sqlite-fs workflow

From `PLAN.md`, the test-first workflow was: write test specs → debate →
write impl specs → debate → regenerate → run.

With the trace skill, we add one step **between test specs and impl specs**:

1. Write test engspec(s).
2. Write first-draft impl engspec(s).
3. **For every test function, run `/engspec-trace`. Every UNCLEAR verdict
   is a spec gap that must be fixed before moving on.** Every FAIL is a
   disagreement that must be resolved.
4. When every test in a module traces to PASS, run `engspec_tester`
   adversarial debate — now with much higher signal, because the cheap
   gaps are already closed.
5. Regenerate code. Run the actual tests. They should pass, because the
   traces already showed they would.

Trace-PASS is a weaker guarantee than code-passing — but it's catchable
before any code exists, which is when it's cheap.

---

## Open questions

- **How do we handle test functions with multiple assertions?** One
  assertion per trace, or combined? Proposal: one trace file per test
  function, one `## Assertion evaluation` section per assertion block,
  verdict is the AND (PASS only if all assertions PASS; UNCLEAR if any
  is UNCLEAR; otherwise FAIL).
- **Parametrized tests?** Each parameter row gets its own trace; they live
  in `traces/<test-function>/<param-id>.trace.md`.
- **Should the generator refuse to emit PASS if any frame used a spec
  whose `source_commit` is newer than the trace's `traced_at`?** Probably
  yes — stale specs mean stale traces.
- **Do we allow "hypothetical" traces — traces against specs that don't
  exist yet?** No. The generator must fail cleanly if an impl spec is
  missing; that's an UNCLEAR with a suggested new spec, not a trace.
- **Who runs `/engspec-verify-trace` and when?** Proposal: automatically,
  as part of CI, over every `*.trace.md` in the repo. A TRACE_INVALID is
  a blocker the same way a failing test is.

---

## First concrete deliverables

1. Write `~/.claude/skills/engspec-trace/TRACE_FORMAT.md` — extract the
   format section of this plan into a standalone reference doc.
2. Write `~/.claude/skills/engspec-trace/SKILL.md` — the generator prompt,
   referencing TRACE_FORMAT.md.
3. Write one worked example — `examples/pass.trace.md` — by hand, to
   validate the format's readability.
4. Write `~/.claude/skills/engspec-verify-trace/SKILL.md` — the verifier
   prompt. Shorter than the generator.
5. Dogfood: use the new skill to trace the very first sqlite-fs test
   (`test_mkfs_creates_empty_fs` from the vertical slice). If the format
   is awkward for that test, fix the format before writing nine more.
