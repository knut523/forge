"""forge council — a model-agnostic panel that gates plans and checks intent.

Two jobs Knut named:
  1. FEATURE PLANNING — before building, a panel critiques a proposed plan: does it
     actually serve the goal, what's missing, what's overbuilt? (plan_review)
  2. INTENT CHECK — after building, the panel judges whether what was built serves the
     intended reason, not merely whether it compiles. (intent_check)

Design mirrors the Prometheus council and plan-to-pr's "loop until the council is happy":
each member forms an INDEPENDENT opinion (blind to the others), then a chair synthesises
a consensus with must-fix / should-fix conditions and a verdict. The caller loops,
folding in must-fix items, until PASS. Panel = every enabled model (minimax, glm, claude,
…), so it is model-agnostic and grows as models are added. Chair defaults to the strongest
model but is overridable so iteration rounds can use a cheap chair and final gates the
strong one.
"""
from __future__ import annotations

import json

from ..config import llm
from ..config.store import ConfigStore


def _extract_json(text):
    # Lazy import — engine imports council, so a top-level import here would be circular.
    from .engine import _extract_json as _ej
    return _ej(text)

COUNCILLOR_SYSTEM = (
    "You are ONE member of a software review council with an independent vote. Judge the "
    "item on its own merits and be honest — dissent is valuable, do not defer to an "
    "imagined majority. Focus on whether it genuinely serves the stated goal, not on "
    "surface polish. Respond with ONLY a JSON object:\n"
    '{"verdict":"pass|concerns|fail","concerns":["specific, actionable"],'
    '"suggestions":["concrete improvement"],"rationale":"two sentences"}'
)

CHAIR_SYSTEM = (
    "You are the chair of a software review council. You are given the item and each "
    "member's independent opinion. Synthesise the consensus WITHOUT rubber-stamping: a "
    "concern raised by even one member that is clearly real must be carried. Separate "
    "must-fix (blocks approval) from should-fix (non-blocking). Verdict is 'pass' only if "
    "there are no must-fix items. Respond with ONLY a JSON object:\n"
    '{"verdict":"pass|concerns|fail","must_fix":["..."],"should_fix":["..."],'
    '"summary":"what the council concluded, three sentences max"}'
)


def _panel(cfg: ConfigStore, models: list[str] | None) -> list[dict]:
    enabled = [m for m in cfg.list_models() if m["enabled"]]
    if models:                                   # explicit override wins
        want = {n.lower() for n in models}
        return [m for m in enabled if m["name"].lower() in want] or enabled
    # Otherwise prefer models Knut curated onto the panel with the 'council' role;
    # fall back to every enabled model so a council always convenes.
    seated = [m for m in enabled if str(m.get("role", "")).lower() == "council"]
    return seated or enabled


def _tok(cfg: ConfigStore, m: dict) -> str | None:
    from .pr_review import _tok as t
    return t(cfg, m)


def deliberate(cfg: ConfigStore, title: str, body: str, *, models: list[str] | None = None,
               chair: str | None = None, on_event=None) -> dict:
    """Run one council round over `body`. Returns {verdict, must_fix, should_fix, summary,
    opinions}. `models` restricts the panel; `chair` names the synthesiser (else strongest)."""
    ev = on_event or (lambda *a, **k: None)
    panel = _panel(cfg, models)
    # Quorum safety (council condition 2026-09-25): the chair is always panel[0] and we
    # proceed with however many voices exist — there is NO fixed-quorum wait, so a missing
    # panelist (e.g. glm not yet configured) can never deadlock. 0 models / 0 parseable
    # opinions degrade to a clean 'fail' return, and the pipeline callers wrap this in
    # try/except (best-effort), so the council can never block a build.
    if not panel:
        return {"verdict": "fail", "must_fix": ["no models registered"], "opinions": []}
    opinions = []
    for m in panel:
        ev("council", "info", f"{m['name']} deliberating…")
        text, meta = llm.complete(m, _tok(cfg, m), COUNCILLOR_SYSTEM,
                                  f"{title}\n\n{body}", max_tokens=8000)
        op = _extract_json(text or "") or {}
        if op:
            op["_member"] = m["name"]
            opinions.append(op)
        else:
            ev("council", "info", f"{m['name']} returned no parseable opinion")
    if not opinions:
        return {"verdict": "fail", "must_fix": ["no usable opinions"], "opinions": []}
    # Chair: strongest model unless overridden (strongest = first in list_models order,
    # which forge keeps reviewer→engineer→rest).
    chair_m = (next((m for m in panel if m["name"] == chair), None) if chair
               else None) or panel[0]
    packed = json.dumps([{k: v for k, v in o.items()} for o in opinions], ensure_ascii=False)
    ev("council", "info", f"{chair_m['name']} (chair) synthesising {len(opinions)} opinion(s)")
    ctext, _ = llm.complete(chair_m, _tok(cfg, chair_m), CHAIR_SYSTEM,
                            f"{title}\n\nITEM:\n{body[:6000]}\n\nMEMBER OPINIONS:\n{packed}",
                            max_tokens=8000)
    con = _extract_json(ctext or "") or {}
    verdict = str(con.get("verdict", "")).lower()
    if verdict not in ("pass", "concerns", "fail"):
        # derive from members if the chair slipped the contract
        verdict = ("fail" if any(o.get("verdict") == "fail" for o in opinions)
                   else "concerns" if any(o.get("verdict") == "concerns" for o in opinions)
                   else "pass")
    return {"verdict": verdict, "must_fix": con.get("must_fix", []),
            "should_fix": con.get("should_fix", []), "summary": con.get("summary", ""),
            "chair": chair_m["name"], "panel": [m["name"] for m in panel],
            "opinions": opinions}


def plan_review(cfg: ConfigStore, goal: str, plan_text: str, **kw) -> dict:
    """Does this plan serve the goal? Gaps, overbuild, wrong approach."""
    body = (f"GOAL (what the user actually wants):\n{goal}\n\n"
            f"PROPOSED PLAN:\n{plan_text}\n\n"
            "Judge: does this plan achieve the goal with the smallest sound change? "
            "Flag missing requirements, scope creep / overbuild, and wrong-approach risks.")
    return deliberate(cfg, "COUNCIL — plan review", body, **kw)


def intent_check(cfg: ConfigStore, goal: str, built_summary: str, evidence: str = "",
                 **kw) -> dict:
    """Does what was built serve the intended reason — not just 'does it run'."""
    body = (f"GOAL (the intended reason):\n{goal}\n\n"
            f"WHAT WAS BUILT:\n{built_summary}\n\n"
            + (f"EVIDENCE (diff / tests / findings):\n{evidence[:5000]}\n\n" if evidence else "")
            + "Judge: does the result actually serve the intended reason? Not 'does it "
            "compile' — does it deliver the user value the goal describes, and did it avoid "
            "solving the wrong problem? Name anything that drifts from intent.")
    return deliberate(cfg, "COUNCIL — intent check", body, **kw)
