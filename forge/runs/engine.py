"""The coder's run loop.

Three phases work today — Capture, Impact, Plan. The rest are declared in
`PHASES` and rendered greyed out, because a view that shows only the finished
part of the road lets a short run pass for a complete one.

Every phase follows the same contract: announce what it is about to do, emit
what it actually found with the evidence attached, and end with one honest
summary line. A phase that could not do its job says so and the run continues
degraded — a missing model is a fact to record, not a crash.
"""
from __future__ import annotations

import threading
import traceback

from ..config.store import ConfigStore
from ..config import llm
from ..indexer import query as Q
from ..indexer import wayfinder as W
from ..indexer.store import Store as IndexStore
from .store import RunStore

PLAN_SYSTEM = (
    "You are a senior engineer planning a change to an existing codebase. "
    "You are given real, indexed facts about the repository — not guesses. "
    "Produce a short SPEC (intent, in scope, out of scope, acceptance criteria) "
    "and then an ordered PLAN where every step names the files to touch, the "
    "change, and how it will be verified. Prefer the smallest change that meets "
    "the ask. If the facts are insufficient to plan safely, say exactly what is "
    "missing instead of inventing it. Be concise and concrete."
)


def _pick_model(cfg: ConfigStore) -> tuple[dict | None, str | None, str]:
    """The engineer-role model, its credential, and why if there isn't one."""
    models = [m for m in cfg.list_models() if m["enabled"]]
    if not models:
        return None, None, "no models registered — add one in Settings"
    chosen = next((m for m in models if m["role"] == "engineer"), None)
    if chosen is None:
        return None, None, ("no model has the 'engineer' role — assign it in "
                            "Settings so a run knows which to use")
    token = cfg.get_secret(chosen["secret_key"]) if chosen["secret_key"] else None
    if chosen["secret_key"] and token is None:
        return None, None, (f"model {chosen['name']!r} points at credential "
                            f"{chosen['secret_key']!r}, which is not set")
    return chosen, token, ""


def _capture(rs: RunStore, rid: str, idx: IndexStore, repo: str) -> dict:
    rs.set_phase(rid, "capture")
    rs.emit(rid, "capture", "step", f"Reading the index for {repo}")

    ov = Q.overview(idx, repo)
    rs.emit(rid, "capture", "metric", "Codebase shape", {
        "files": ov["file_count"], "symbols": ov["symbol_count"],
        "edges": ov["ref_count"], "resolved_pct": ov["resolved_pct"],
        "branch": ov.get("branch"), "head": (ov.get("head_sha") or "")[:8],
        "languages": [f"{l['lang']} ({l['files']} files, {l['loc']} loc)"
                      for l in ov["languages"]],
    })
    if ov["parse_errors"]:
        rs.emit(rid, "capture", "warn",
                f"{len(ov['parse_errors'])} file(s) failed to parse — blind spots",
                ov["parse_errors"], level="warn")

    hot = Q.hotspots(idx, repo, 10)
    rs.emit(rid, "capture", "finding", "Load-bearing symbols",
            [{"symbol": h["qualname"], "at": f"{h['path']}", "referenced": h["refs"]}
             for h in hot])

    orph = Q.orphans(idx, repo, 15)
    if orph:
        rs.emit(rid, "capture", "finding",
                f"{len(orph)} exported symbol(s) nothing references",
                [{"symbol": o["qualname"], "at": f"{o['path']}:{o['start_line']}"}
                 for o in orph])

    rs.emit(rid, "capture", "done",
            f"Grounded in {ov['file_count']} files and {ov['symbol_count']} symbols")
    return {"overview": ov, "hotspots": hot}


def _impact(rs: RunStore, rid: str, idx: IndexStore, repo: str,
            target: str | None) -> dict:
    rs.set_phase(rid, "impact")
    if not target:
        links = W.repo_links(idx)
        mine = [l for l in links if l["from"] == repo]
        rs.emit(rid, "impact", "info", "No target named — checking repo-level coupling only",
                {"hint": "name a symbol or file to get a precise blast radius"})
        if mine:
            rs.emit(rid, "impact", "finding", "This repo is coupled to", mine)
        else:
            rs.emit(rid, "impact", "done", "No cross-repo coupling detected")
        return {"links": mine}

    try:
        rep = W.impacts(idx, repo, symbol=None if "/" in target else target,
                        path=target if "/" in target else None)
    except ValueError as e:
        rs.emit(rid, "impact", "warn", f"Could not resolve target {target!r}",
                {"error": str(e)}, level="warn")
        return {}

    rs.emit(rid, "impact", "step", f"Tracing {target}", {
        "symbols_in_scope": rep["target"]["match_count"],
        "repos_searched": rep["indexed_repos"]})

    local = rep["local_callers"]
    rs.emit(rid, "impact", "finding" if local else "info",
            f"{len(local)} caller(s) inside {repo}",
            local[:40] or None)

    if not rep["cross_repo"]:
        rs.emit(rid, "impact", "done", "No evidence of impact outside this repo")
    for c in rep["cross_repo"]:
        lvl = "warn" if c["confidence"] == "high" else "info"
        rs.emit(rid, "impact", "impact",
                f"Also touches {c['repo']} ({c['confidence']} confidence)",
                c["signals"], level=lvl)
    for n in rep["notes"]:
        rs.emit(rid, "impact", "info", n)
    return rep


def _plan(rs: RunStore, rid: str, cfg: ConfigStore, goal: str, repo: str,
          cap: dict, imp: dict) -> None:
    rs.set_phase(rid, "plan")
    model, token, why = _pick_model(cfg)

    facts = _facts_block(repo, cap, imp)
    if model is None:
        rs.emit(rid, "plan", "warn", "No engineer model available — planning skipped",
                {"reason": why,
                 "grounding_that_would_have_been_used": facts}, level="warn")
        rs.emit(rid, "plan", "info",
                "The grounding above is real and complete; only the drafting step "
                "is missing. Register a model with the 'engineer' role and re-run.")
        return

    rs.emit(rid, "plan", "step", f"Drafting with {model['name']}",
            {"provider": model["provider"], "model": model["model_id"]})
    text, meta = llm.complete(
        model, token, PLAN_SYSTEM,
        f"GOAL\n{goal}\n\nGROUNDED FACTS ABOUT THE REPOSITORY\n{facts}")
    if text is None:
        rs.emit(rid, "plan", "error", "The model call failed", meta, level="error")
        return
    rs.emit(rid, "plan", "plan", "Spec and plan", {"text": text, **meta})
    rs.emit(rid, "plan", "done", "Plan drafted — review it before Build runs")


def _facts_block(repo: str, cap: dict, imp: dict) -> str:
    ov = cap.get("overview", {})
    lines = [f"repo: {repo}",
             f"branch: {ov.get('branch')}  head: {(ov.get('head_sha') or '')[:8]}",
             f"size: {ov.get('file_count')} files, {ov.get('symbol_count')} symbols",
             "languages: " + ", ".join(
                 f"{l['lang']} ({l['files']} files)" for l in ov.get("languages", [])),
             "",
             "most-referenced symbols (changing these is high risk):"]
    for h in cap.get("hotspots", [])[:10]:
        lines.append(f"  {h['qualname']}  ({h['refs']} refs)  {h['path']}")
    cross = imp.get("cross_repo") or []
    if cross:
        lines += ["", "cross-repo impact detected:"]
        for c in cross:
            sigs = ", ".join(f"{k}×{len(v)}" for k, v in c["signals"].items())
            lines.append(f"  {c['repo']} [{c['confidence']}] via {sigs}")
    if imp.get("local_callers"):
        lines += ["", f"callers of the target inside this repo: "
                      f"{len(imp['local_callers'])}"]
    return "\n".join(lines)


def execute(run_id: str, index_db: str) -> None:
    """Run the pipeline. Never raises — a crash is recorded as a failed run."""
    rs = RunStore()
    cfg = ConfigStore()
    idx = None
    try:
        run = rs.get(run_id)
        if run is None:
            return
        repo, goal, target = run["repo"], run["goal"], run["target"]
        rs.emit(run_id, None, "info", "Run started",
                {"repo": repo, "goal": goal, "target": target})
        idx = IndexStore(index_db, readonly=True)

        cap = _capture(rs, run_id, idx, repo)
        imp = _impact(rs, run_id, idx, repo, target)
        _plan(rs, run_id, cfg, goal, repo, cap, imp)

        rs.emit(run_id, None, "info", "Reached the end of the implemented phases",
                {"next": "Build, Verify, Review, Approve and PR are not built yet"})
        rs.finish(run_id, "done")
    except Exception as e:
        rs.emit(run_id, None, "error", f"Run failed: {type(e).__name__}",
                {"error": str(e)[:400], "trace": traceback.format_exc()[-1200:]},
                level="error")
        rs.finish(run_id, "failed", f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if idx is not None:
            idx.close()
        cfg.close()
        rs.close()


def start(run_id: str, index_db: str) -> None:
    threading.Thread(target=execute, args=(run_id, index_db), daemon=True).start()
