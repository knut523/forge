"""Review an external GitHub pull request and draft a comment.

Two depths, both read-only by contract (READ token + models; never a write token):

  light — one strong-model structured pass. Fast.
  full  — a plan-to-pr-style pipeline: every enabled model runs an independent
          multi-angle finder pass, the candidates are de-duplicated in Python,
          then the strongest model runs an adversarial verify pass that refutes
          weak findings and scores confidence.

Both feed the model forge's own cross-repo impact (the one thing GitHub cannot
tell you) and the PR's existing comments (so the review builds on the discussion
instead of repeating it). Posting the resulting draft is a separate, explicitly
human-approved step (the comment endpoint in api/app.py).
"""
from __future__ import annotations

import json
import re

from ..config import llm
from ..config.store import ConfigStore
from ..indexer import prs as PRs
from . import review_grounding as RG
from . import review_exec as REx
from . import review_sast as SAST
from .engine import _extract_json

# Modern review models carry large context, so the budget should hold a real
# feature PR whole rather than clip it — a clipped diff is why a review misses the
# back half of a big change. The cap is a runaway backstop, not a design tool;
# noise (lockfiles, vendored/generated code) is dropped BEFORE it, so the budget
# goes to code a human would actually review.
_DIFF_CAP = 300000
_NOISE = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
          "Cargo.lock", "composer.lock", "go.sum", "Gemfile.lock",
          ".min.js", ".min.css", ".map", "/dist/", "/build/", "/vendor/",
          "__snapshots__", ".snap")


def _prep_diff(diff: str) -> tuple[str, str]:
    """Drop vendored/generated/lockfile hunks so the model's budget goes to real
    code, then cap. Returns (prepared_diff, note)."""
    sections = re.split(r"(?=^diff --git )", diff, flags=re.M)
    kept, dropped = [], []
    for s in sections:
        if not s.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", s)
        path = m.group(2) if m else ""
        if path and any(n in path for n in _NOISE):
            dropped.append(path)
        else:
            kept.append(s)
    out = "".join(kept) if kept else diff
    notes = []
    if dropped:
        notes.append(f"omitted {len(dropped)} generated/lock file(s) "
                     f"({', '.join(sorted(set(dropped))[:5])})")
    if len(out) > _DIFF_CAP:
        out = out[:_DIFF_CAP] + (f"\n\n… diff truncated at {_DIFF_CAP // 1000}k chars — "
                                 f"this change is unusually large; review the remainder directly …")
        notes.append(f"still truncated at {_DIFF_CAP // 1000}k chars")
    return out, ("; ".join(notes) if notes else "")

_ANGLES = (
    "(1) line-by-line correctness: inverted/wrong conditions, off-by-one, "
    "null/undefined on a reachable path, missing await, swallowed errors, "
    "wrong-variable copy-paste, falsy-zero; "
    "(2) removed behavior: for every deleted or changed line, name the invariant "
    "it enforced and check the new code re-establishes it; "
    "(3) cross-file / contract: changed signatures, return shapes or exceptions "
    "that break callers — use the cross-repo impact and the ENCLOSING CODE provided; "
    "(4) security and data integrity: injection, auth, secrets written to logs, "
    "URL paths or telemetry, and personal data leaving its lane; "
    "(5) acceptance: for EACH acceptance criterion, is it actually met by this diff — "
    "trace it, an unmet criterion is a finding; "
    "(6) reachability: is new code actually called, and does a new test actually run "
    "in CI — see the MECHANICAL FINDINGS; "
    "(7) conventions: does the change break a rule in the REPO CONVENTIONS or a lesson "
    "in REPO MEMORY provided."
)

_GATE = (
    "This is a MERGE GATE, not a courtesy read. Trace concrete inputs through the code — "
    "a plausible failing input is a finding. An unmet acceptance criterion, a new symbol "
    "nothing calls, a new test no CI script runs, PII in a log, or a violated repo rule "
    "is a finding even when the code 'looks fine'. Do NOT reassure or pad. Carry every "
    "MECHANICAL FINDING provided unless you can prove it wrong. Build on the existing "
    "comments: do not repeat a point already raised, and flag any raised concern that "
    "looks unaddressed. If you end with no findings, you must have traced each acceptance "
    "criterion and the riskiest inputs explicitly — silence is not evidence of safety. "
    "A property the PR's rationale deliberately intends, or an import of a file a sibling "
    "PR provides, is not a defect."
)

LIGHT_SYSTEM = (
    "You are a rigorous senior reviewer at a merge gate. Review for real defects across "
    "these angles: " + _ANGLES + " " + _GATE + " Respond with ONLY a JSON object:\n"
    '{"summary": "one paragraph", "findings": [{"severity": "high|medium|low", '
    '"file": "path", "line": 0, "detail": "what is wrong and why, with a concrete '
    'failure scenario"}], "verdict": "pass|pass-with-concerns|changes-requested"}'
)

FINDER_SYSTEM = (
    "You are a rigorous senior reviewer hunting real defects at a merge gate. Work "
    "systematically through EACH angle and surface every genuine issue: " + _ANGLES
    + " " + _GATE + " Respond with ONLY JSON:\n"
    '{"findings": [{"severity": "high|medium|low", "file": "path", "line": 0, '
    '"angle": "correctness|removed|contract|security|acceptance|reachability|convention", '
    '"detail": "concrete failure scenario or the unmet criterion"}]}'
)

VERIFY_SYSTEM = (
    "You are an adversarial verifier at a merge gate. You are given the change's goal, "
    "its ACCEPTANCE CRITERIA, the REPO CONVENTIONS and REPO MEMORY, the ENCLOSING CODE, "
    "the SIBLING FILES earlier PRs add, the diff, the MECHANICAL FINDINGS, and candidate "
    "findings. For each candidate decide: CONFIRMED (real defect), PLAUSIBLE (real on a "
    "realistic path), or REFUTED. REFUTE only what is factually wrong, provably "
    "impossible, already handled, an INTENTIONAL design choice the rationale states, an "
    "import of a file a SIBLING PR provides, or a concern OUTSIDE the acceptance criteria. "
    "Keep every MECHANICAL FINDING unless you can show it is wrong. Additionally emit a "
    "finding for any acceptance criterion this diff does not meet. Drop every REFUTED, "
    "merge duplicates, assign confidence 0-100.\n"
    "Verdict discipline: 'changes-requested' if ANY acceptance criterion is unmet OR any "
    "confirmed defect remains; 'pass' ONLY when every criterion is met and no real defect "
    "remains; reserve 'pass-with-concerns' for a real but non-blocking defect, never for "
    "an unmet criterion or an acknowledged trade-off. Respond with ONLY JSON:\n"
    '{"summary": "one paragraph", "findings": [{"severity": "high|medium|low", '
    '"file": "path", "line": 0, "detail": "...", "confidence": 0, '
    '"verdict": "confirmed|plausible"}], '
    '"verdict": "pass|pass-with-concerns|changes-requested"}'
)


def _models(cfg: ConfigStore) -> list[dict]:
    """Enabled models, strongest first (reviewer role, then engineer, then rest)."""
    order = {"reviewer": 0, "engineer": 1}
    ms = [m for m in cfg.list_models() if m["enabled"]]
    ms.sort(key=lambda m: order.get(m.get("role"), 2))
    return ms


def _tok(cfg: ConfigStore, m: dict) -> str | None:
    return cfg.get_secret(m["secret_key"]) if m["secret_key"] else None


def _impact_lines(impact: dict) -> str:
    if not impact.get("available"):
        return f"(cross-repo impact unavailable: {impact.get('why', 'unknown')})"
    reaches = impact.get("reaches", [])
    if not reaches:
        return f"No cross-repo impact across {impact.get('checked', 0)} checked file(s)."
    return "\n".join(f"- reaches {r['repo']} ({r['confidence']}) via "
                     f"{', '.join(r.get('files', [])[:6])}" for r in reaches)


def _comments_lines(comments: list[dict]) -> str:
    if not comments:
        return "(none)"
    return "\n\n".join(f"@{c.get('author') or '?'}: {c.get('body', '')}"
                       for c in comments)


def _context(pr: dict, owner: str, repo: str, number: int, impact: dict,
             comments: list[dict], diff: str, grounding: str = "") -> str:
    return (
        f"PR: {owner}/{repo}#{number} — {pr.get('title', '')}\n"
        f"Author: {pr.get('author', '?')}  {pr.get('head', '')} → {pr.get('base', '')}\n"
        f"Changed files: {pr.get('changed_files', '?')}  "
        f"(+{pr.get('additions', '?')} / -{pr.get('deletions', '?')})\n\n"
        f"Description:\n{(pr.get('body') or '(none)')[:2000]}\n\n"
        f"forge cross-repo impact:\n{_impact_lines(impact)}\n\n"
        + (grounding + "\n\n" if grounding else "")
        + f"Existing comments on this PR:\n{_comments_lines(comments)}\n\n"
        f"Unified diff:\n{diff}"
    )


def _memory_block(repo_name: str | None, touched: list[str]) -> str:
    """Relevant repo memories (conventions + past rejection lessons) for the files
    this PR touches. Best-effort: memory being unavailable must not sink a review."""
    if not repo_name:
        return ""
    try:
        from .memory import MemoryStore
        ms = MemoryStore()
        try:
            mems = ms.recall(repo_name, touched=touched, cap_tokens=1400)
        finally:
            ms.close()
    except Exception:
        return ""
    return "\n".join(
        f"- [{m.get('kind', '')}] {m.get('title', '')}: {(m.get('body') or '')[:300]}"
        for m in (mems or [])[:12])


def _apply_grounding(review_obj: dict, g: dict) -> dict:
    """Fold the deterministic seed findings into the review and gate the verdict.

    Seeds (an unreached new symbol, an unwired new test) are decided from the clone
    and CI config, not guessed, so a model must not be able to drop them silently.
    A high-severity seed forces changes-requested."""
    seeds = g.get("seeds", [])
    if not seeds:
        return review_obj
    findings = review_obj.setdefault("findings", [])
    have = {((f.get("file") or "").lower(), (f.get("detail") or "")[:40].lower())
            for f in findings}
    for s in seeds:
        key = ((s.get("file") or "").lower(), (s.get("detail") or "")[:40].lower())
        if key not in have:
            findings.append({k: v for k, v in s.items() if k != "seed"})
    if any(s.get("severity") == "high" for s in seeds):
        review_obj["verdict"] = "changes-requested"
    return review_obj


def _dedup(findings: list[dict]) -> list[dict]:
    """Collapse near-duplicates (same file, ~same line, same gist) — plain code,
    so a finding raised by two finders costs one verify slot, not two."""
    seen, out = set(), []
    for f in findings:
        if not isinstance(f, dict):
            continue
        try:
            line = int(f.get("line") or 0) // 5
        except (TypeError, ValueError):
            line = 0
        key = (str(f.get("file", "")).strip().lower(), line,
               str(f.get("detail", ""))[:40].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# Two axes, reported separately and never merged: Spec (does the change deliver and
# verify what it claims) vs Standards (is the code itself correct and clean).
_SPEC_ANGLES = {"acceptance", "reachability", "ci-wiring"}


def _axis(f: dict) -> str:
    return "Spec" if str(f.get("angle", "")).lower() in _SPEC_ANGLES else "Standards"


def _draft(owner: str, repo: str, number: int, review: dict, meta: dict,
           n_comments: int) -> str:
    order = {"high": 0, "medium": 1, "low": 2}
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}

    def _fmt(f: dict) -> str:
        sev = str(f.get("severity", "low")).lower()
        loc = f.get("file", "")
        if f.get("line"):
            loc += f":{f['line']}"
        conf = f" _(confidence {f['confidence']})_" if f.get("confidence") else ""
        return (f"- {icon.get(sev, '⚪')} **{sev}** "
                f"{('`' + loc + '` — ') if loc else ''}{f.get('detail', '')}{conf}")

    findings = sorted(review.get("findings", []),
                      key=lambda f: order.get(str(f.get("severity", "low")).lower(), 3))
    lines = [f"**Review of `{owner}/{repo}#{number}`**", ""]
    if review.get("summary"):
        lines += [review["summary"], ""]
    if findings:
        lines.append(f"**Findings ({len(findings)})**")
        # Spec axis first — an unmet requirement outranks a clean-code nit.
        for axis in ("Spec", "Standards"):
            group = [f for f in findings if _axis(f) == axis]
            if group:
                label = ("Spec — does it deliver & verify what it claims"
                         if axis == "Spec" else "Standards — is the code correct & clean")
                lines.append(f"_{label}_")
                lines += [_fmt(f) for f in group]
                lines.append("")
    else:
        lines += ["No blocking issues found.", ""]
    if review.get("verdict"):
        lines += [f"**Verdict:** {review['verdict']}", ""]
    how = (f"full check — {len(meta.get('finders', []))} finder model(s) "
           f"({', '.join(meta.get('finders', []))}) + adversarial verify"
           if meta.get("mode") == "full"
           else f"light check — {', '.join(meta.get('finders', []))}")
    considered = (f", considered {n_comments} prior comment(s)" if n_comments else "")
    lines.append(f"<sub>Drafted by forge · {how}{considered}. Verify before relying "
                 f"on it.</sub>")
    return "\n".join(lines)


def review(cfg: ConfigStore, owner: str, repo: str, number: int,
           read_token: str | None, index_store, local_repo: str | None,
           mode: str = "light", on_event=None) -> dict:
    """(findings, draft, ...) — or {'error'/'why'}. Read-only: never posts.

    on_event(phase, kind, title[, detail]) is called at each step so a run can
    stream what the review is doing; it defaults to a no-op for direct callers."""
    ev = on_event or (lambda *a, **k: None)
    if not read_token:
        return {"why": f"no read token for org {owner!r} — add one in Settings"}
    models = _models(cfg)
    if not models:
        return {"why": "no models registered — add one in Settings"}
    ev("setup", "info", f"Reviewing {owner}/{repo}#{number} · {mode} check")
    try:
        pr = PRs.detail(owner, repo, number, read_token)
        diff = PRs.diff(owner, repo, number, read_token)
    except PRs.GHError as e:
        return {"error": str(e)}
    comments = PRs.comments(owner, repo, number, read_token)
    impact = (PRs.impact_for(pr, index_store, local_repo) if local_repo
              else {"available": False, "why": "this repo is not indexed locally"})
    diff, diffnote = _prep_diff(diff)
    if diffnote:
        ev("context", "info", f"diff prepared: {diffnote}")
    reaches = [r["repo"] for r in impact.get("reaches", [])] if impact.get("available") else []
    changed = [f.get("path") for f in pr.get("files", []) if f.get("path")]
    g = RG.build(local_repo, pr.get("body", ""), diff, changed,
                 memory_block=_memory_block(local_repo, changed))
    if local_repo:
        xr = REx.run_tests(local_repo, diff, changed)
        if xr.get("seeds"):
            g["seeds"] = g.get("seeds", []) + xr["seeds"]
        ev("exec", "info", "ran touched tests: " + (xr.get("note") or xr.get("why") or "—"))
        sx = SAST.scan(local_repo, diff, changed)
        if sx.get("seeds"):
            g["seeds"] = g.get("seeds", []) + sx["seeds"]
        ev("sast", "info", "SAST: " + (sx.get("note") or sx.get("why") or "—"))
    gnote = []
    if g["acceptance"]["criteria"]:
        gnote.append(f"{len(g['acceptance']['criteria'])} acceptance criterion/-a")
    if g["seeds"]:
        gnote.append(f"{len(g['seeds'])} mechanical finding(s)")
    if g["has_conventions"]:
        gnote.append("conventions")
    ev("context", "info",
       f"Diff {len(diff)//1000}k chars · {pr.get('changed_files', '?')} files · "
       f"{len(comments)} prior comment(s)"
       + (f" · impact reaches {', '.join(reaches)}" if reaches else "")
       + (f" · grounding: {', '.join(gnote)}" if gnote else ""))
    base = _context(pr, owner, repo, number, impact, comments, diff, g["block"])

    if mode == "full":
        finders = models[:2]                       # up to 2 distinct models
        ev("find", "info", f"{len(finders)} finder model(s): "
                           f"{', '.join(m['name'] for m in finders)}")
        cand, used = [], []
        for m in finders:
            ev("find", "info", f"{m['name']}: scanning the diff…")
            text, _ = llm.complete(m, _tok(cfg, m), FINDER_SYSTEM, base, max_tokens=4000)
            used.append(m["name"])
            got = (_extract_json(text) or {}).get("findings", []) if text else []
            cand += got
            ev("find", "info", f"{m['name']}: {len(got)} candidate finding(s)")
        raw = len(cand)
        cand = _dedup(cand)
        ev("find", "info", f"{raw} candidate(s) → {len(cand)} after dedup")
        vm = models[0]
        ev("verify", "info", f"adversarial verify with {vm['name']}…")
        vtext, vmeta = llm.complete(
            vm, _tok(cfg, vm), VERIFY_SYSTEM,
            base + "\n\nCANDIDATE FINDINGS:\n" + json.dumps(cand)[:8000],
            max_tokens=4000)
        vp = _extract_json(vtext or "") or {}
        if vtext is None and not cand:
            return {"error": vmeta.get("error", "the model calls failed")}
        review_obj = {"summary": vp.get("summary", ""),
                      "findings": vp.get("findings", cand),  # fall back to deduped
                      "verdict": vp.get("verdict")}
        ev("verify", "info", f"{len(review_obj['findings'])} finding(s) survived verify")
        meta = {"mode": "full", "finders": used, "verifier": vm["name"]}
    else:
        m = models[0]
        ev("review", "info", f"{m['name']}: reviewing…")
        text, mmeta = llm.complete(m, _tok(cfg, m), LIGHT_SYSTEM, base, max_tokens=4000)
        if text is None:
            return {"error": mmeta.get("error", "the model call failed")}
        parsed = _extract_json(text) or {}
        if "findings" not in parsed:
            parsed = {"summary": text.strip()[:4000], "findings": [], "verdict": None}
        review_obj = {"summary": parsed.get("summary", ""),
                      "findings": parsed.get("findings", []),
                      "verdict": parsed.get("verdict")}
        meta = {"mode": "light", "finders": [m["name"]]}

    review_obj = _apply_grounding(review_obj, g)
    draft = _draft(owner, repo, number, review_obj, meta, len(comments))
    return {"findings": review_obj["findings"], "summary": review_obj["summary"],
            "verdict": review_obj["verdict"], "draft": draft, "mode": mode,
            "models": meta.get("finders"), "verifier": meta.get("verifier"),
            "prior_comments": len(comments), "impact": impact,
            "acceptance": g["acceptance"], "seeds": len(g["seeds"])}


def review_built(cfg: ConfigStore, repo: str, goal: str, diffs: list[dict],
                 impact_note: str = "", on_event=None, sibling_ctx: str | None = None) -> dict:
    """Full multi-model + adversarial review of a locally-built diff, before its
    PR exists. Same passes as the GitHub PR review, minus the fetch — so the coder
    gets the same scrutiny at its gate that an external PR gets."""
    ev = on_event or (lambda *a, **k: None)
    models = _models(cfg)
    if not models:
        return {"why": "no models registered — add one in Settings"}
    changed = [d.get("path") for d in diffs if d.get("path")]
    diff = "\n".join(f"+++ b/{d.get('path')}\n{d.get('diff','')}" for d in diffs)
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + f"\n\n… diff truncated at {_DIFF_CAP // 1000}k chars …"
    g = RG.build(repo, goal, diff, changed, memory_block=_memory_block(repo, changed))
    exec_diff = "\n".join(d.get("diff", "") for d in diffs)
    xr = REx.run_tests(repo, exec_diff, changed)
    if xr.get("seeds"):
        g["seeds"] = g.get("seeds", []) + xr["seeds"]
    ev("review", "info", "ran touched tests: " + (xr.get("note") or xr.get("why") or "—"))
    sx = SAST.scan(repo, exec_diff, changed)
    if sx.get("seeds"):
        g["seeds"] = g.get("seeds", []) + sx["seeds"]
    ev("review", "info", "SAST: " + (sx.get("note") or sx.get("why") or "—"))
    base = (f"Change under review in {repo} (a locally-built change, not yet a PR).\n"
            f"Goal: {goal}\n\nforge cross-repo impact:\n{impact_note or '(none)'}\n\n"
            + (g["block"] + "\n\n" if g["block"] else "")
            + (f"SIBLING FILES — earlier PRs in this feature add these; imports of "
               f"them are CORRECT (they exist once merged), do NOT flag them as "
               f"missing or undefined:\n{sibling_ctx}\n\n" if sibling_ctx else "")
            + f"Unified diff:\n{diff}")
    finders = models[:2]
    cand, used = [], []
    for m in finders:
        ev("review", "info", f"{m['name']}: scanning the built diff…")
        text, _ = llm.complete(m, _tok(cfg, m), FINDER_SYSTEM, base, max_tokens=4000)
        used.append(m["name"])
        got = (_extract_json(text) or {}).get("findings", []) if text else []
        cand += got
        ev("review", "info", f"{m['name']}: {len(got)} candidate finding(s)")
    raw = len(cand)
    cand = _dedup(cand)
    vm = models[0]
    ev("review", "info", f"{raw} candidate(s) → {len(cand)} deduped · verify with {vm['name']}…")
    vtext, _ = llm.complete(vm, _tok(cfg, vm), VERIFY_SYSTEM,
                            base + "\n\nCANDIDATE FINDINGS:\n" + json.dumps(cand)[:8000],
                            max_tokens=4000)
    vp = _extract_json(vtext or "") or {}
    review_obj = _apply_grounding(
        {"findings": vp.get("findings", cand), "summary": vp.get("summary", ""),
         "verdict": vp.get("verdict")}, g)
    return {"findings": review_obj["findings"], "summary": review_obj["summary"],
            "verdict": review_obj["verdict"], "models": used, "verifier": vm["name"],
            "acceptance": g["acceptance"], "seeds": len(g["seeds"])}


def _local_repo(store, owner: str, repo: str) -> str | None:
    row = store.db.execute("SELECT name FROM repos WHERE origin=?",
                           (f"{owner}/{repo}",)).fetchone()
    return row["name"] if row else None


def run(run_id: str, index_db: str, owner: str, repo: str, number: int,
        mode: str) -> None:
    """Thread body: run the review as a run, streaming each step as an event so
    the UI can show what it is doing. Read-only; the draft is delivered as the
    final 'result' event, never posted."""
    from ..indexer.store import Store as IndexStore
    from .store import RunStore
    from ..config.store import ConfigStore, GITHUB_READ, org_read_key
    rs = RunStore()
    try:
        rs.set_phase(run_id, "setup")
        cfg = ConfigStore()
        store = IndexStore(index_db, readonly=True)
        try:
            token = cfg.get_secret(org_read_key(owner)) or cfg.get_secret(GITHUB_READ)
            local = _local_repo(store, owner, repo)

            def emit(phase, kind, title, detail=None):
                rs.set_phase(run_id, phase)
                rs.emit(run_id, phase, kind, title, detail)

            out = review(cfg, owner, repo, number, token, store, local, mode,
                         on_event=emit)
        finally:
            store.close()
            cfg.close()
        problem = out.get("error") or out.get("why")
        if problem:
            rs.emit(run_id, "done", "error", problem)
            rs.finish(run_id, "failed", error=problem)
        else:
            rs.emit(run_id, "done", "result",
                    f"Review ready · {out.get('verdict') or '—'} · "
                    f"{len(out.get('findings', []))} finding(s)", out)
            rs.finish(run_id, "done")
    except Exception as e:                       # a thread must never die silently
        try:
            rs.emit(run_id, "done", "error", f"{type(e).__name__}: {e}")
            rs.finish(run_id, "failed", error=str(e))
        except Exception:
            pass
    finally:
        rs.close()


def start(run_id: str, index_db: str, owner: str, repo: str, number: int,
          mode: str) -> None:
    import threading
    threading.Thread(target=run,
                     args=(run_id, index_db, owner, repo, number, mode),
                     daemon=True).start()
