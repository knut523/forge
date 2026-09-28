"""Concern-decomposition review (parity plan, Phase 1) — the core intervention.

The single-blob adversarial reviewer finds ~1 defect on a 14-defect infra PR because it
reviews the whole PR in one pass (P0: minimax 0/14, claude 1/14 — a STRATEGY ceiling, not
a model one). A senior reviewer instead splits the PR into distinct CONCERNS (migration
safety, access-control, identifier case-folding across write+read, retention, PII,
idempotency) and checks each in depth. This does that:

  1. ENUMERATE the PR's concerns from the diff (one cheap model call).
  2. For each concern, RETRIEVE its cross-file surface from the index (the readers/writers
     of the symbols it touches) so an invariant that spans files — the #175 case-folding-
     between-retention-and-read class — is visible, not blind.
  3. Run ONE focused adversarial reviewer PER concern, in PARALLEL on cheap models, each
     seeded with its concern + cross-file map + the shared read-only clone's tools.
  4. Dedup, then a recall-biased REFUTE pass; only confirmed/plausible survive.

Model-agnostic and parallel (D₀ substrate — validated by P0: cheap ≈ strong under the old
strategy, so breadth on cheap models is sound). Read-only: shares one clone, never writes.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import llm
from ..indexer import query as Q
from . import review_agent as AGENT
from . import review_exec as REx
from . import review_sast as SAST
from . import repomap as RepoMap
from .engine import _extract_json

# Fixed concern taxonomy (steal: PR-Agent's always-checked dimensions + OWASP + Google
# eng-practices). These ALWAYS run so coverage never depends on the model remembering a
# category. `blocking` types ratchet the verdict; advisory ones do not (council: don't block
# on style). The model-enumerated PR-specific concerns are added on top.
FIXED_TAXONOMY = [
    {"key": "security", "blocking": True,
     "title": "Security & input validation — injection, auth bypass, unsafe deserialization, "
              "SSRF, secrets, unsafe eval/exec, path traversal"},
    {"key": "data_integrity", "blocking": True,
     "title": "Data integrity & migration safety — destructive/irreversible ops, wrong rows "
              "affected, migration unsafe across the rollout window, lost writes"},
    {"key": "cross_file", "blocking": True,
     "title": "Cross-file invariants — a value written in one place and read in another under "
              "a mismatched assumption (normalization, encoding, timezone, units)"},
    {"key": "idempotency", "blocking": True,
     "title": "Idempotency & side-effects — retries/duplicates, non-idempotent writes, ordering "
              "and concurrency races, dedupe-key stability"},
    {"key": "pii", "blocking": True,
     "title": "PII & logging — personal data written to logs/telemetry, over-broad storage, "
              "no per-person erasure path"},
    {"key": "access_control", "blocking": True,
     "title": "Access control & authz — a new route/endpoint missing its gate, privilege or "
              "tenant-isolation checks, IDOR"},
    {"key": "errors", "blocking": False,
     "title": "Error & empty-state handling — swallowed errors, unhandled null/empty, wrong "
              "status codes, partial-failure fallbacks that lose data"},
    {"key": "tests", "blocking": False,
     "title": "Tests — is the changed behaviour covered by a test that actually runs in CI"},
    {"key": "performance", "blocking": False,
     "title": "Performance — N+1, sequential scans, blocking work on a hot path"},
    {"key": "maintainability", "blocking": False,
     "title": "Maintainability — dead code, duplication, mysterious naming, needless complexity"},
]
_BLOCKING_KEYS = {t["key"] for t in FIXED_TAXONOMY if t["blocking"]}


def _tok(cfg, m):
    from .pr_review import _tok as t
    return t(cfg, m)

CONCERN_SYSTEM = (
    "You are triaging a pull request into distinct review CONCERNS so each can be reviewed "
    "in depth separately. From the diff and changed files, list the specific risk areas a "
    "senior reviewer would check ONE AT A TIME. Each concern is a concrete risk tied to the "
    "files it lives in — e.g. 'identifier case-folding consistency between the retention job "
    "and the read/search path', 'the new admin route must gate servicePii', 'the migration "
    "must be safe across the rollout window', 'PII written to logs/telemetry', 'idempotency "
    "of the new webhook handler'. Be specific to THIS PR; never generic ('code quality'). "
    "Respond ONLY JSON:\n"
    '{"concerns":[{"title":"specific risk","files":["path", ...],'
    '"why":"the concrete failure this risks"}]}\n'
    "Return 4–8 concerns covering the PR's real risk surface (migration/schema, "
    "access-control/authz, data consistency & cross-file invariants, PII/logging, "
    "idempotency/retries, error/empty-state handling) wherever the diff touches them."
)


def _concern_brief(system_extra: str) -> str:
    return (
        "You are a hostile senior reviewer assigned ONE concern of a pull request. Investigate "
        "ONLY this concern, in depth, and try to BREAK it. Use your tools: read the files it "
        "touches AND the code on the other side of any invariant (a value written in one file "
        "and read in another — check both), grep for callers, and reason about concrete failing "
        "inputs. Assume there IS a defect in this area and find it. " + system_extra +
        "\nEmit ONE JSON action per turn (a tool call, or your verdict):\n"
        '  {"tool":"read_file|grep|list_dir","path":"..."}\n'
        '  {"done":true,"findings":[{"severity":"high|medium|low","file":"path","line":0,'
        '"detail":"the concrete defect in THIS concern, its trigger, and the fix"}]}\n'
        "If after investigating this concern is genuinely clean, return done with empty "
        "findings — but investigate first, do not conclude from the diff alone."
    )


def _changed_symbols(diff: str) -> list[str]:
    """Names that appear on changed lines — the symbols whose cross-file use matters."""
    names: list[str] = []
    seen = set()
    for ln in diff.splitlines():
        if not (ln.startswith("+") or ln.startswith("-")) or ln.startswith(("+++", "---")):
            continue
        for m in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b", ln):
            if m in seen or m in ("const", "return", "import", "export", "function",
                                  "await", "async", "true", "false", "null", "undefined"):
                continue
            seen.add(m)
            names.append(m)
    return names[:40]


def _crossfile_map(store, repo_name: str, concern: dict, diff: str) -> str:
    """For the concern's symbols, list where they are defined and who reads/writes them —
    so a cross-file invariant is on the table without the reviewer having to find it."""
    if store is None:
        return ""
    syms = _changed_symbols(diff)
    files = set(concern.get("files") or [])
    lines: list[str] = []
    used = 0
    for name in syms:
        if used >= 8:
            break
        try:
            defs = Q.find_symbol(store, name, repo_name)
        except Exception:
            defs = []
        if not defs:
            continue
        # prefer symbols defined in (or near) the concern's files
        d = next((x for x in defs if x.get("path") in files), defs[0])
        try:
            cs = Q.callers(store, d["id"], limit=8)
        except Exception:
            cs = []
        if not cs:
            continue
        where = ", ".join(sorted({c.get("path", "") for c in cs})[:6])
        lines.append(f"  `{name}` (def {d.get('path')}) is used in: {where}")
        used += 1
    if not lines:
        return ""
    return ("CROSS-FILE MAP (both sides of possible invariants — read the USE sites, not just "
            "the changed file):\n" + "\n".join(lines))


def _recover_concerns(text: str) -> list[dict]:
    """Salvage concerns from a truncated think block: numbered '**Title**: why' lines."""
    out = []
    for m in re.finditer(r"(?m)^\s*\d+\.\s*\*\*(.+?)\*\*[:\-–]?\s*(.*)$", text):
        title = m.group(1).strip()
        why = m.group(2).strip()[:200]
        if 4 <= len(title) <= 120:
            out.append({"title": title, "files": [], "why": why or title})
        if len(out) >= 8:
            break
    return out


def _enumerate(cfg, model, token, diff, changed_files, goal) -> list[dict]:
    user = (f"Goal: {(goal or '')[:800]}\nChanged files ({len(changed_files)}): "
            f"{', '.join(changed_files)}\n\nUnified diff:\n{AGENT._balanced_diff(diff, 40000)}")
    # Headroom matters: minimax emits a long <think> before the JSON; 2000 tokens got eaten
    # by the reasoning and the JSON never arrived (0 concerns → blob fallback → 0 findings).
    text, _ = llm.complete(model, token, CONCERN_SYSTEM, user, max_tokens=16000)
    obj = _extract_json(text or "") or {}
    concerns = obj.get("concerns")
    if isinstance(concerns, list) and concerns:
        return concerns
    # Fallback: recover concerns from a numbered/bulleted think block if the JSON was
    # truncated — the reasoning usually lists them even when the JSON does not arrive.
    return _recover_concerns(text or "")


REFUTE_SYSTEM = (
    "You are a recall-biased VERIFIER. Given a PR and a candidate finding, default to PLAUSIBLE; "
    "REFUTE only when constructibly wrong (quote the contradicting line / cite the guard / quote "
    "documented intent). 'seems unlikely' is NOT grounds to refute. Respond ONLY JSON:\n"
    '{"verdict":"confirmed|plausible|refuted","confidence":0,"reason":"cite the code"}')


def _dedup(findings: list[dict]) -> list[dict]:
    out, seen = [], set()
    for f in findings:
        key = (str(f.get("file", "")).strip(),
               re.sub(r"\d+", "", str(f.get("detail", ""))[:80].lower()))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _build_concerns(cfg, breadth_model, breadth_tok, diff, changed_files, goal, ev,
                    max_enumerated=5):
    """Fixed taxonomy (guaranteed coverage, model-INDEPENDENT) + model-enumerated PR-specific
    concerns (capped — the council's hard call-cap so the bridge doesn't bottleneck)."""
    enumerated = _enumerate(cfg, breadth_model, breadth_tok, diff, changed_files, goal)[:max_enumerated]
    concerns = []
    for t in FIXED_TAXONOMY:
        concerns.append({"key": t["key"], "title": t["title"], "blocking": t["blocking"],
                         "files": changed_files, "why": "fixed-taxonomy coverage", "fixed": True})
    for c in enumerated:
        c.setdefault("key", "enumerated"); c.setdefault("blocking", True)  # PR-specific = treat as blocking
        c["fixed"] = False
        concerns.append(c)
    ev("concerns", "info", f"{len(FIXED_TAXONOMY)} taxonomy + {len(enumerated)} enumerated "
       f"= {len(concerns)} concern(s)")
    return concerns


def _seed_findings(repo_name, diff, changed_files, ev):
    """Deterministic seeds (semgrep) folded in as guaranteed findings — a known pattern the
    LLM misses is still caught by the scanner."""
    try:
        s = SAST.scan(repo_name, diff, changed_files)
    except Exception:
        return []
    seeds = s.get("seeds") or []
    out = []
    for sd in seeds:
        out.append({"severity": sd.get("severity", "medium"), "file": sd.get("file", ""),
                    "line": sd.get("line", 0), "detail": f"[semgrep] {sd.get('detail', sd)}",
                    "concern": "security", "key": "security", "blocking": True,
                    "confidence": 90, "seed": True})
    if out:
        ev("seeds", "info", f"{len(out)} deterministic seed finding(s) folded in")
    return out


def review(cfg, model: dict, token: str | None, repo_name: str, diff: str,
           changed_files: list[str], goal: str = "", grounding: str = "",
           prepared: dict | None = None, store=None, on_event=None,
           max_concurrency: int = 5, breadth_model: dict | None = None,
           use_repomap: bool = True) -> dict:
    """Scaled concern-decomposed review. `model` = the DEPTH reviewer (finds defects, e.g.
    claude); `breadth_model` = the cheap reviewer for advisory concerns + enumeration (e.g.
    minimax) — the council's cost-guard so the fixed taxonomy doesn't bottleneck the bridge.
    Fixed taxonomy guarantees coverage; deterministic seeds + coverage-tracking + severity-aware
    merge complete it. Returns {findings, verdict, summary, concerns, coverage}."""
    ev = on_event or (lambda *a, **k: None)
    depth_tok = token
    bm = breadth_model or model
    btok = _tok(cfg, bm) if breadth_model else token

    concerns = _build_concerns(cfg, bm, btok, diff, changed_files, goal, ev)

    own = False
    if not (prepared and prepared.get("dir")):
        prep = REx._prepare(repo_name, diff)
        if prep.get("error"):
            return {"error": prep["error"]}
        prepared, own = prep, True
    root = prepared["dir"]

    # Graph-ranked cross-file context (Aider repo-map over forge's index), computed once.
    xctx = ""
    if use_repomap and store is not None:
        try:
            xctx = RepoMap.rank_context(store, repo_name, changed_files, diff)
        except Exception:
            xctx = ""

    def _one(c: dict) -> list[dict]:
        # cost guard: blocking/enumerated concerns get the DEPTH model; advisory get breadth.
        use_model, use_tok = ((model, depth_tok) if c.get("blocking")
                              else (bm, btok))
        xmap = xctx or _crossfile_map(store, repo_name, c, diff)
        cg = (f"THE CONCERN: {c.get('title')}\nWHY IT'S RISKY: {c.get('why')}\n"
              f"FILES: {', '.join(c.get('files') or [])}\n\n"
              + (xmap + "\n\n" if xmap else "")
              + (grounding[:4000] if grounding else ""))
        try:
            r = AGENT.review(cfg, use_model, use_tok, repo_name, diff,
                             c.get("files") or changed_files,
                             goal=f"CONCERN: {c.get('title')}", grounding=cg,
                             prepared={"dir": root, "scratch": None}, max_steps=12,
                             system_override=_concern_brief(
                                 f"FILES for this concern: {', '.join(c.get('files') or [])}."))
            fs = r.get("findings", []) or []
            for f in fs:
                f["concern"] = c.get("title")
                f["concern_key"] = c.get("key")
                f["blocking_concern"] = bool(c.get("blocking"))
            return fs
        except Exception as e:
            ev("concerns", "info", f"concern '{str(c.get('key'))}' errored: {type(e).__name__}")
            return []

    all_findings: list[dict] = list(_seed_findings(repo_name, diff, changed_files, ev))
    reviewed_files: set = set()
    try:
        with ThreadPoolExecutor(max_workers=max_concurrency) as ex:
            futs = {ex.submit(_one, c): c for c in concerns}
            for fut in as_completed(futs):
                got = fut.result() or []
                c = futs[fut]
                for f in (c.get("files") or changed_files):
                    reviewed_files.add(f)
                ev("concerns", "info", f"[{c.get('key')}] → {len(got)} finding(s)")
                all_findings += got
    finally:
        if own and prepared.get("scratch"):
            import shutil
            shutil.rmtree(prepared["scratch"], ignore_errors=True)

    # coverage: every changed file should have been in some concern's scope
    uncovered = [f for f in changed_files if f not in reviewed_files]
    coverage = {"changed": len(changed_files), "reviewed": len(reviewed_files & set(changed_files)),
                "uncovered": uncovered}
    if uncovered:
        ev("coverage", "warn", f"{len(uncovered)} changed file(s) not covered by any concern")

    # dedup + recall-biased refute (seeds are pre-confirmed, skip refute for them)
    cand = _dedup(all_findings)
    to_verify = [f for f in cand if not f.get("seed")]
    ev("refute", "info", f"{len(all_findings)} raw → {len(cand)} deduped; verifying {len(to_verify)}")
    confirmed = [f for f in cand if f.get("seed")]
    for f in to_verify[:30]:
        user = (f"PR in `{repo_name}`. Concern: {f.get('concern','')}\n"
                f"CANDIDATE: {json.dumps({k: f.get(k) for k in ('severity','file','line','detail')}, ensure_ascii=False)[:1000]}\n\n"
                f"Diff:\n{AGENT._balanced_diff(diff, 12000)}")
        vt, _ = llm.complete(model, depth_tok, REFUTE_SYSTEM, user, max_tokens=8000)
        v = _extract_json(vt or "") or {}
        if str(v.get("verdict", "")).lower() in ("confirmed", "plausible"):
            f["confidence"] = int(v.get("confidence", 70) or 70)
            confirmed.append(f)

    sev = {"high": 0, "medium": 1, "low": 2}
    confirmed.sort(key=lambda f: sev.get(str(f.get("severity", "low")).lower(), 3))
    # severity-aware merge (council): block only on findings from BLOCKING concern-types;
    # advisory-concern findings (maintainability/style/perf/tests) never ratchet to block.
    blocking_hits = [f for f in confirmed if f.get("blocking_concern")
                     and str(f.get("severity", "")).lower() in ("high", "medium")]
    if blocking_hits:
        verdict = "changes-requested"
    elif confirmed:
        verdict = "pass-with-concerns"
    else:
        verdict = "pass"
    return {"findings": confirmed, "verdict": verdict,
            "summary": (f"{len(concerns)} concerns ({len(FIXED_TAXONOMY)} taxonomy) → "
                        f"{len(confirmed)} confirmed ({len(blocking_hits)} blocking)"),
            "concerns": [c.get("key") for c in concerns], "coverage": coverage,
            "model": model.get("name")}
