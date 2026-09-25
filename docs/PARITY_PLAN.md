# forge → parity → superiority with Christoph (plan v2, council-revised)

## The goal (verbatim, not downgraded)
forge's reviewer reaches **parity** with Christoph (bizarrochris) — matches his blocking
findings — on deep infra PRs, then **superiority** — additionally catches real defects he
missed. Measured, not asserted.

## The gap, from tonight's evidence (not opinion)
- Mechanical/structural PRs: forge is already strong — #190 **2/3**, correct verdict.
- Deep infra PRs: forge is weak — #181 **1/14**, #175 minimax **0/14** / claude found 3→confirmed 1.
- The matrix proved the **reviewer, not the coder, is the bottleneck**: a blind reviewer makes the loop a no-op.
- Root causes of the miss (why 1/14 not 12/14):
  1. **Blob review.** One adversarial pass over a 55-file PR finds ~1 thing. Christoph mentally
     splits the PR into concerns (migration safety, access control, case-folding consistency,
     retention, PII, idempotency) and checks each. One pass ≠ N concern checks.
  2. **Shallow retrieval.** forge reads files it stumbles onto; it does not systematically pull
     the *other side* of a cross-file invariant (the #175 bug was case-folding inconsistent
     *between* the retention job and the read path — you only see it if you read both).
  3. **No durable domain knowledge.** Christoph knows olaf/WirStrom. forge's only proxy is the
     thin Angle-E past-comment injection.
  4. **Model ceiling + quota.** minimax can't review deep; claude can partially but is slow,
     single-threaded on the bridge, and rate-limits under load.

## Plan v2 (revised with the council — replan folded in)

The council rejected v1's sequencing on four counts, all correct: (1) Phase A without B is a
*crippled* intervention — concern fan-out blind to cross-file context replicates shallow review
N times and still misses the invariant bugs (#175 case-folding) that define the weakness, so
**A and B must be one phase**; (2) you cannot gate on a benchmark that does not exist — **E must
come first, as a minimal slice**; (3) per-concern reviewers serialize back into one slow blob on
the single-threaded bridge unless the parallel shim exists — **D's minimal shim comes first**;
(4) we are *assuming* the bottleneck is strategy, not model capability — **prove it first with a
cheap controlled test** before any architectural rework.

### Phase 0 — Cheap disambiguation, BEFORE any rework  *(do this first — it decides the rest)*
- **P0a · Controlled model-swap test.** Take one deep-infra PR with known ground truth (#181,
  14 findings). Run the *same* adversarial prompt with different reviewer models (minimax, claude,
  and one alternative open model). Measure recall per model.
  - If recall is ~identical and low → the bottleneck is **strategy** (decomposition/retrieval) →
    Phase 1 is the fix.
  - If a stronger model is materially higher → the bottleneck is partly **model capability** →
    Phase 1 *plus* a model-tier decision. This is the honest branch v1 never grappled with.
- **P0b · E₀ held-out benchmark slice.** 3–5 held-out Christoph deep-infra PRs, his blocking
  findings extracted as ground truth, NOT used for any lens-mining. Nothing is gated until E₀ exists.

### Phase 1 — Concern decomposition **powered by** cross-file retrieval  *(merged A+B; the core intervention)*
- Enumerate the PR's distinct **concerns / risk-areas** (migration safety, access-control,
  case-folding across write+read, retention, PII, idempotency), grounded in the diff.
- **For each concern, retrieve its cross-file context first**: pull the readers/writers/callers of
  every changed symbol/table/field via the index (`Q.callers`/`callees`/`blast_radius` already
  exist) + a per-PR **domain brief** (AGENTS.md rules + conventions + Angle-E past Christoph
  comments). Then run **one focused adversarial reviewer per concern, seeded with BOTH sides of
  its invariants** — so the case-folding-between-retention-and-read class is *visible*, not blind.
- **D₀ · minimal ensemble shim (built here, not later):** the per-concern reviewers run in
  **parallel on cheap models** for breadth, so they don't serialize into one slow blob on the bridge.
- Dedup + recall-biased REFUTE guard precision. Gate on E₀.
- **Hypothesis:** #181's ~14 findings span ~5–6 concern areas; concern-fan-out *with cross-file
  context* is what turns 1/14 into most-of-14. (Contingent on P0a saying strategy is the lever.)

### Phase 2 — Christoph-corpus learning loop  *(the superiority engine)*
- Mine the **355 indexed Christoph comments** into recurring **domain lenses** ("identifier
  case-folding must match across write+read", "a new admin route must gate servicePii", "a
  migration must be safe across the rollout window").
- Each review runs the relevant learned lenses as extra concern-finders.
- **Continuous eval:** after each review, auto-diff forge's findings vs his actual comments; every
  miss becomes a new lens. This is how forge *surpasses* him — it never forgets a pattern he flags.
  Validate lenses on E₀'s held-out set only (no overfitting).

### Phase 3 — Full ensemble depth  *(quota-smart, once P0a/Phase 1 justify it)*
- Breadth on cheap models (concern enum, lenses, shallow finds) in parallel; **depth on claude
  only for flagged/ambiguous concerns**, not the whole PR → small, fast, parallel claude calls
  instead of one 9-minute blob that rate-limits. If P0a showed a model ceiling, this is where the
  stronger reviewer model is slotted in (per-phase configurable).

### Phase E (continuous) — Parity / superiority gate
- **PARITY** = forge matches ≥ [TARGET, proposed 80%] of Christoph's blocking findings on the E₀
  held-out set. **SUPERIORITY** = forge also surfaces real findings he missed (human-validated).
- Each phase must show a measured recall lift on E₀ before the next is trusted; no parity claim
  until the held-out number says so.

## Sequencing & rationale (final, council-passed)
**P0 (model-swap test + E₀ benchmark) → [P0a result gates the substrate] → Phase 1 (merged
concern-decomp + cross-file retrieval; D₀ substrate chosen by P0a) → measure on E₀ → Phase 2
(learning loop) → measure → Phase 3 (ensemble depth).**
P0 decides whether strategy alone can reach parity or a stronger model is also needed — cheaply,
before rework. Phase 1 is the core intervention (targets parity). Phase 2 targets superiority.

### First build increment (council refinement — P0 is a true gate, NOT bundled with P1)
- **Ship P0 first and read its result before committing P1's substrate.** P0a's model-swap
  outcome decides whether the D₀ parallel-cheap-model architecture is even the right substrate:
  a severe model ceiling means cheap-model breadth is the wrong bet and Phase 3's stronger model
  moves earlier.
- **P1's model-agnostic scaffolding may proceed in parallel** with P0 — concern decomposition and
  cross-file retrieval are valuable regardless of P0a's outcome (they are model-independent).
- **D₀'s design waits for P0a.** Do not finalize the parallel-cheap-model shim until the model-swap
  test says cheap-model breadth is sound.

## Anti-overbuild (ponytail) + risks
- Cap N concerns per PR; ensemble keeps the fan-out cheap.
- More finders → more false positives: recall-biased REFUTE + the confidence threshold guard precision.
- Learned lenses can overfit Christoph's style: validate on the held-out set only.
- Don't build the #27 embedding layer until B proves the index isn't enough.
- Quota: the bridge is the scarce resource; D's ensemble is what makes A/B/C affordable.

## Open decisions (for Knut)
1. Parity target % (proposed 80% of blocking findings on the E₀ held-out set).
2. If P0a shows a model ceiling (a stronger model materially out-recalls the current ones),
   are we willing to add/pay for a stronger reviewer model, or is strategy-only the constraint?
3. Scope for the first build session: P0 + Phase 1 is the natural first increment (proves
   whether strategy closes the gap and delivers the core intervention behind a real gate).

## P0 RESULT (2026-09-25) — strategy is the bottleneck
- **P0a model-swap on #181 (14 real findings):** minimax **0/14**, claude **1/14**. Both models
  miss ~13/14 under the current single-blob adversarial strategy; the 1-vs-0 gap is noise, not a
  capability difference. → **Bottleneck = STRATEGY, not model capability.** A stronger model alone
  won't close the gap; the process must decompose (Phase 1). Re-test model choice AFTER Phase 1.
- **Consequence:** D₀'s cheap-model substrate is validated (claude ≈ minimax under current
  strategy) → **Phase 1 can be built + iterated on minimax** (no claude rate-limit), claude
  reserved for a post-Phase-1 recall re-test.
- **P0b:** E₀ held-out set + `FORGE_E0` harness built (`docs/E0_heldout.md`); full E₀ recall run
  pending claude quota reset.

## PRIOR ART — don't rebuild what's already built well (Knut's steer, 2026-09-25)
The parity capabilities are largely solved problems in mature OSS. Verified by research:
- **Qodo PR-Agent** (Apache-2, open source, model-configurable): specialized multi-agent review
  (bug / logic-gap / missing-test / security / risk = our "concern decomposition"), cross-file +
  dependency retrieval, AND **indexes past PR diffs/comments/discussions to filter findings**
  (= our P2 Christoph-corpus learning loop). Plus diff compression + a /review /improve command UI.
  → P1 *and* P2 are largely already built here.
- **Aider repo-map** (MIT; ports: RepoMapper (py), goldfish (go)): tree-sitter symbol graph →
  **personalized PageRank** (chat/PR symbols biased ×10–50) → token-budgeted render of the most
  central signatures. This is the *right* way to do P1's cross-file retrieval — and **forge already
  has the symbol graph** (symbols/refs/callers in its index); it only lacks the PageRank ranking +
  budgeted render. Small, high-value port.
- **Greptile** (codebase-graph PR review) + **CodeRabbit** ("learnings" from past review comments):
  commercial, but they validate the graph-retrieval and learn-from-past-comments theses.

### P1 hand-rolled experiment result (the evidence for pivoting)
Concern decomposition on #181 produced **8 excellent, Christoph-grade concerns** — but each
per-concern minimax reviewer confirmed **~0 findings**. Decomposition (the strategy) works; the
per-concern *reviewer execution* (turning a concern into an articulated defect) is the hard part —
exactly what PR-Agent's specialized agents already do. Hand-rolling it further = reinventing PR-Agent.

### Revised approach (measurement-first, same discipline as P0)
1. **Adopt Aider's repo-map** ranking over forge's EXISTING index (personalized PageRank + budgeted
   render) — small port, replaces the ad-hoc `_crossfile_map`. Proven, cheap, forge has the graph.
2. **Measurement spike on PR-Agent** BEFORE integrate-vs-port: install PR-Agent, point it at #181 +
   E₀ with our models, measure its recall vs Christoph. "Others built it well" is a hypothesis to
   *verify on our repos* (the vendor-claims-until-reproduced lesson), not assume.
3. **Then the council decides integrate-vs-port-vs-native** with real numbers: if PR-Agent
   materially out-recalls forge on E₀ → integrate it (it's model-agnostic, fits forge) or port its
   specialized-agent + past-comment patterns; if not, the gap is deeper and we learned it cheaply.

## Council trail
- v1 → **replan**: merge A+B (fan-out is crippled without cross-file retrieval); pull E and D
  forward as minimal slices (can't gate on a missing benchmark; per-concern reviewers serialize
  without the parallel shim); run a controlled model-swap test first (don't assume strategy is the
  bottleneck — prove it). All four folded into v2.
- v2 → **PASS** (confidence 0.75), with one refinement: split the first increment — P0 is a true
  diagnostic gate, NOT bundled with P1. P1's model-agnostic scaffolding may proceed in parallel;
  D₀'s parallel-cheap-model substrate design waits for P0a's model-swap result. Folded in above.
  Plan is council-approved. First action: **P0** (model-swap test + E₀ benchmark).
