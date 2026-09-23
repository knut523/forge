# NEXT — the two agreed pieces of work

Read `AGENTS.md` first. Pick ONE of these and say which, so two people don't
land on the same files. They were chosen because they don't overlap:
task A is `forge/runs/memory.py` + `engine.py`, task B is `forge/runs/ship.py`.

---

## A. Memory rework — pins and SHA supersession

**Why.** Two defects in what shipped:

1. `add()` upserts on `(repo, kind, title)`, so relearning a lesson
   **overwrites** the old body. This is the exact move mem0 removed in 2026 —
   their words, "this is where context got destroyed." It loses the transition
   ("we used Jest, we now use Vitest, because ESM"), which is more useful than
   either endpoint, and it silently clobbers an unrelated lesson that happened
   to generate the same title.
2. `for_prompt()` injects every active memory into every prompt. Cline users
   report ~300k tokens after five iterations doing this, and caching does not
   save it because the re-read is per task. Coding gotchas are overwhelmingly
   path-local; they should be retrieved against the files the change touches.

**Researched decision: do NOT make memory graph-shaped.** No credible published
evidence it beats flat plus decent retrieval — on their own numbers the flat
baselines win, and one study measured graph memory actively hurting, because
decomposing an emphasized statement into triples loses the emphasis. A lesson
*is* an emphasized statement. Keep the prose verbatim.

**The change, in full:**

1. Columns on `memories`: `pins TEXT` (JSON array of strings),
   `valid_from_sha`, `valid_until_sha`, `superseded_by`. Add via the existing
   `PRAGMA table_info` migration pattern.

2. Pins are **strings resolved at query time, never a foreign key**:
   `file:src/auth/session.py`, `symbol:AuthService.validate_tenant`,
   `route:/api/v1/orders`. An unresolvable pin must be inert, not wrong — same
   invariant as the index. Prefer file pins; symbols churn harder. Only record
   a pin when the attachment is unambiguous.

3. **Git SHA is the temporal axis, not wall-clock.** A repo convention becomes
   false when code changes, not when time passes. Replace the destructive
   upsert: insert a new row, stamp the old one's `valid_until_sha`, link via
   `superseded_by`. Default recall filters `valid_until_sha IS NULL`. History
   stays queryable as "what did we believe at commit abc123".

4. **Two retrieval channels, RRF-fused, hard token cap** — replaces
   inject-everything:
   - *pin channel*: exact match of pins against the files/symbols this run
     touches. Weight it roughly 2×; always injected.
   - *text channel*: FTS5 over title+body for memories with no matching pin.
   Cap by **tokens, not count** — 25 verbose rows are 10× the budget of 25
   terse ones.

5. **Split the injection points.** Full ranked set at PLAN time; at BUILD time
   only the rows matching the approved plan's touched files. Both already exist
   as separate call sites in `engine.py` — this is nearly free.

6. **Conflict detection, zero LLM calls.** Two *active* memories sharing a pin,
   written at different SHAs, in a contradiction-prone kind → surface as a
   candidate pair for a human. **Never auto-resolve.** GraphRAG's "ask the LLM
   to make the contradiction go away" is the anti-pattern: it drops the
   superseded claim with no record.

7. Optional, only if 1–6 land cleanly: for a run touching symbol S, also
   surface lessons pinned to S's **direct callees** (1 hop, cap 5, ranked below
   direct hits). That is blast radius, over call edges the index already has.
   No new graph.

**Explicitly rejected** — do not build these: LLM entity extraction over lesson
bodies (~10% junk precision into a precision-first store); communities, Leiden,
MMR or cross-encoder reranking (scale machinery for 10^5 nodes, we have
hundreds); wall-clock bi-temporality; any FK from memory into symbol rows.

**Done when:** a lesson learned twice produces two rows with the first
superseded; `GET /api/memory/preview?repo=` shows a *scoped* set rather than
everything; and a build-phase prompt contains only memories relevant to the
files being touched.

---

## B. Branch stacking for overlapping items in a feature split

**Why.** Epic items execute one at a time, but every item's PR branches from
`dev` and none are merged in between. When two items touch the same file — and
Assess already detects exactly this and warns — the second PR conflicts on
merge. The warning is real and currently has no remedy.

**The change:** in `runs/ship.py::open_pr`, when the item being shipped depends
on an earlier item that touched any of the same files, branch from **that
item's head** instead of `dev`, and open the PR against that branch rather than
`dev`. Record the base chain on the epic so the UI can show it as a stack.

Keep the honest fallback: if the parent branch is gone, or the dependency did
not actually touch a shared file, branch from `dev` as today and say why.

**Done when:** a two-item split where both edit the same file produces two PRs
that merge cleanly in order, and the run view shows what each is based on.
