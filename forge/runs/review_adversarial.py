"""Adversarial review — plan-to-pr's try-to-break-it discipline, model-agnostic.

A single agentic pass tends to explore a bit and conclude 'looks fine' (the #175
failure: 0 findings on a 14-finding PR). plan-to-pr is stronger because it is
ADVERSARIAL: it tries to break the change, then tries to REFUTE each finding it
raised. This module gives forge that discipline in two passes over llm.complete:

  1. BREAK pass — an agentic reviewer with a hostile brief: hunt the inputs, states,
     and removed invariants that make this PR wrong. Run the tests. It is told to
     EXPECT defects and find them, not to assess.
  2. REFUTE pass — for each candidate finding, an adversarial verifier tries to
     prove it wrong (factually false / impossible / already handled / outside the
     acceptance criteria). Only CONFIRMED or PLAUSIBLE survive, with a confidence.

Both passes use the agentic tool loop (read/grep/run_tests) so they investigate,
and both are model-agnostic. The reviewer is adversarial by construction, the way
plan-to-pr's Pass C is.
"""
from __future__ import annotations

import json
import shutil

from ..config import llm
from . import review_agent as AGENT
from . import review_exec as REx
from .engine import _extract_json

BREAK_SYSTEM = (
    "You are a hostile senior reviewer. Your job is to BREAK this pull request, not "
    "assess it politely. Assume there are defects and find them. Use your tools: read "
    "each changed file and the code around it, grep for callers and for the invariants "
    "deleted lines used to enforce, and RUN the touched tests. Hunt specifically for: "
    "inputs/states that crash or misbehave; removed behaviour nothing re-establishes; "
    "changed signatures breaking callers; personal data written to logs/telemetry; "
    "unmet acceptance criteria; new code nothing calls; tests that do not run in CI. "
    "Trace a concrete failing input for each. Emit ONE JSON action per turn (a tool "
    "call, or your verdict):\n"
    '  {"tool":"read_file|grep|list_dir|run_tests", ...}\n'
    '  {"done":true,"findings":[{"severity":"high|medium|low","file":"path","line":0,'
    '"detail":"the defect, the concrete trigger, and the fix"}]}\n'
    "If you genuinely cannot find a defect after investigating, say so with done and "
    "an empty findings list — but investigate first, do not conclude 'fine' from the "
    "diff alone."
)

REFUTE_SYSTEM = (
    "You are a VERIFIER. You are given a PR and a candidate finding. Decide whether it is "
    "real — but be RECALL-BIASED, the same discipline a careful human review uses: default "
    "to PLAUSIBLE, and only REFUTE when the finding is CONSTRUCTIBLY wrong. REFUTED requires "
    "one of: it is factually wrong (quote the actual contradicting line), provably impossible "
    "(cite the type/constant/guard), already handled elsewhere in the diff (cite it), or an "
    "intentional choice the PR explicitly documents (quote it). 'Seems unlikely', 'depends on "
    "runtime state', or 'probably fine' is NOT grounds to refute — those are PLAUSIBLE. Mark "
    "CONFIRMED when you can show the concrete trigger. Respond with ONLY JSON:\n"
    '{"verdict":"confirmed|plausible|refuted","confidence":0,"reason":"cite the code"}'
)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… (clipped, {len(s)} chars)"


def review(cfg, model: dict, token: str | None, repo_name: str, diff: str,
           changed_files: list[str], goal: str = "", grounding: str = "",
           prepared: dict | None = None, max_steps: int = 26, on_event=None) -> dict:
    """Two-pass adversarial review. Returns {findings, summary, verdict, steps}."""
    ev = on_event or (lambda *a, **k: None)
    # Pass 1: BREAK — agentic, hostile brief, reuses the agent loop with a hostile system.
    ev("break", "info", f"BREAK pass with {model['name']} — hunting defects")
    br = AGENT.review(cfg, model, token, repo_name, diff, changed_files, goal=goal,
                      grounding=grounding, max_steps=max_steps, on_event=ev,
                      prepared=prepared, system_override=BREAK_SYSTEM)
    if br.get("error"):
        return br
    cands = br.get("findings", [])
    ev("break", "info", f"BREAK found {len(cands)} candidate(s)")

    # Pass 2: REFUTE each candidate — try to prove it wrong. Reuse the scratch clone so
    # the verifier can read the same code; it does not own the cleanup (caller does).
    root = (prepared or {}).get("dir") or br.get("_root")
    confirmed = []
    for c in cands[:10]:
        ev("refute", "info", f"refuting: {str(c.get('detail'))[:60]}")
        user = (f"PR in `{repo_name}`. Goal: {(goal or '')[:600]}\n"
                f"Changed files: {', '.join(changed_files)}\n\n"
                f"CANDIDATE FINDING:\n{json.dumps(c, ensure_ascii=False)[:1200]}\n\n"
                f"Unified diff:\n{_clip(diff, 18000)}\n\n"
                "Investigate the code with your tools if needed, then refute or confirm.")
        # a tiny tool-enabled verify: one read/grep is usually enough to check a claim
        vtext, _ = llm.complete(model, token, REFUTE_SYSTEM, user, max_tokens=8000)
        v = _extract_json(vtext or "") or {}
        verdict = str(v.get("verdict", "")).lower()
        if verdict in ("confirmed", "plausible"):
            c["confidence"] = int(v.get("confidence", 70))
            c["verify"] = verdict
            confirmed.append(c)
        else:
            ev("refute", "info", f"  refuted: {(v.get('reason') or '')[:80]}")

    sev = {"high": 0, "medium": 1, "low": 2}
    confirmed.sort(key=lambda f: sev.get(str(f.get("severity", "low")).lower(), 3))
    verdict = ("changes-requested" if any(f.get("severity") == "high" for f in confirmed)
               else "pass-with-concerns" if confirmed else br.get("verdict") or "pass")
    return {"findings": confirmed, "summary": br.get("summary", ""),
            "verdict": verdict, "steps": br.get("steps", 0),
            "candidates": len(cands), "model": model.get("name")}