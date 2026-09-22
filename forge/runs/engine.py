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

import json
import os
import re
import threading
import traceback
from pathlib import Path

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
          cap: dict, imp: dict) -> dict:
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
        return {"blocked": why}

    rs.emit(rid, "plan", "step", f"Drafting with {model['name']}",
            {"provider": model["provider"], "model": model["model_id"]})
    text, meta = llm.complete(
        model, token, PLAN_SYSTEM,
        f"GOAL\n{goal}\n\nGROUNDED FACTS ABOUT THE REPOSITORY\n{facts}")
    if text is None:
        rs.emit(rid, "plan", "error", "The model call failed", meta, level="error")
        return {"error": meta.get("error")}
    rs.emit(rid, "plan", "plan", "Spec and plan", {"text": text, **meta})
    rs.emit(rid, "plan", "done", "Plan drafted")
    return {"text": text}


BUILD_SYSTEM = (
    "You are a senior engineer implementing an approved plan in an existing "
    "codebase. You are given the plan and the CURRENT CONTENT of the files it "
    "names. Return the COMPLETE new content of every file you change — never a "
    "diff, never an ellipsis, never a placeholder. Do not invent APIs: use only "
    "what the given files and facts show exists. Output ONLY a single JSON "
    'object of exactly this shape, with no prose and no markdown fences:\n'
    '{"files":[{"path":"<repo-relative path>","content":"<full file text>"}],'
    '"notes":"<one short paragraph on what you changed and why>"}'
)

STUB_MARKERS = ("todo", "your code here", "in a real implementation",
                "notimplementederror", "fixme", "placeholder")


def _repo_root(idx: IndexStore, repo: str) -> Path | None:
    """Where the working tree actually is, now — not where it was at index time.

    `repos.root` records the path the indexer saw, which was a container mount
    that may no longer exist. The /repos/<name> convention is the durable
    fallback, and if neither resolves we say so rather than guessing.
    """
    row = idx.db.execute("SELECT root FROM repos WHERE name=?", (repo,)).fetchone()
    for cand in ([Path(row["root"])] if row and row["root"] else []) + \
                [Path(os.environ.get("FORGE_REPOS", "/repos")) / repo]:
        if cand.is_dir():
            return cand
    return None


def _extract_json(text: str) -> dict | None:
    """Models wrap JSON in fences and commentary however they like.

    Reasoning models put a <think> block first, and that block is full of
    braces and quoted code — feed it to a brace matcher and it will happily
    parse some fragment of the model's deliberation as the answer. Strip it
    before looking for anything.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"<think>.*$", "", text, flags=re.S | re.I)   # unterminated = truncated
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:                       # try each '{' as a candidate start
        depth, instr, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if instr:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    instr = False
                continue
            if ch == '"':
                instr, esc = True, False
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _build(rs: RunStore, rid: str, cfg: ConfigStore, idx: IndexStore, repo: str,
           goal: str, plan_text: str, target: str | None) -> dict:
    """Write the change into an isolated workspace. Never touches the source tree."""
    rs.set_phase(rid, "build")
    model, token, why = _pick_model(cfg)
    if model is None:
        rs.emit(rid, "build", "warn", "No engineer model — nothing built",
                {"reason": why}, level="warn")
        return {"blocked": why}

    root = _repo_root(idx, repo)
    if root is None:
        msg = (f"No working tree for {repo!r}. The index records where the repo "
               f"was mounted when it was indexed; that path is gone. Mount it at "
               f"/repos/{repo} (or re-index from there) so Build can read files.")
        rs.emit(rid, "build", "warn", "No working tree available",
                {"reason": msg}, level="warn")
        return {"blocked": msg}

    # Give the model the files the plan is actually about, not the whole repo.
    wanted: list[str] = []
    if target and "/" in target:
        wanted.append(target)
    for m in re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx)", plan_text or ""):
        if m not in wanted:
            wanted.append(m)
    seen: list[dict] = []
    for rel in wanted[:6]:
        p = root / rel
        if not p.is_file():
            hit = idx.db.execute(
                "SELECT f.path FROM files f JOIN repos r ON r.id=f.repo_id"
                " WHERE r.name=? AND f.path LIKE ? ORDER BY LENGTH(f.path) LIMIT 1",
                (repo, f"%{rel}")).fetchone()
            p = root / hit["path"] if hit else p
            rel = hit["path"] if hit else rel
        if p.is_file():
            try:
                body = p.read_text("utf-8", "replace")
            except OSError:
                continue
            seen.append({"path": rel, "content": body[:60000]})
    rs.emit(rid, "build", "step", f"Building with {model['name']}",
            {"files_given_to_the_model": [f["path"] for f in seen] or "none",
             "workspace": f"/work/{rid}"})

    ctx = "\n\n".join(f"--- FILE {f['path']} ---\n{f['content']}" for f in seen)
    text, meta = llm.complete(
        model, token, BUILD_SYSTEM,
        f"GOAL\n{goal}\n\nPLAN\n{plan_text}\n\nCURRENT FILES\n{ctx or '(none supplied)'}",
        max_tokens=24000)
    if text is None:
        rs.emit(rid, "build", "error", "The model call failed", meta, level="error")
        return {"error": meta.get("error")}

    obj = _extract_json(text) or {}
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        # Distinguish "it rambled" from "it was cut off mid-answer" — the fix is
        # different (prompt vs token budget) and the raw tail shows which.
        truncated = "<think>" in text and "</think>" not in text
        rs.emit(rid, "build", "error",
                "The model output was truncated mid-reasoning" if truncated
                else "The model returned no usable files",
                {"truncated": truncated, "chars": len(text),
                 "output_tokens": meta.get("output_tokens"),
                 "tail": text[-1200:]}, level="error")
        return {"error": "build output truncated" if truncated
                         else "unparseable build output"}

    ws = Path(os.environ.get("FORGE_WORK", "/work")) / rid
    written = []
    for f in files:
        if not isinstance(f, dict):
            continue
        rel, content = str(f.get("path", "")).strip(), f.get("content")
        if not rel or not isinstance(content, str):
            continue
        dest = (ws / rel).resolve()
        if not str(dest).startswith(str(ws.resolve())):   # path-escape guard
            rs.emit(rid, "build", "warn", f"Refused a path outside the workspace: {rel}",
                    level="warn")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        before = next((s["content"] for s in seen if s["path"] == rel), None)
        written.append({"path": rel, "lines": content.count("\n") + 1,
                        "bytes": len(content),
                        "change": "modified" if before is not None else "new"})
        rs.emit(rid, "build", "file", f"{'Modified' if before is not None else 'Created'} {rel}",
                {"lines": content.count("\n") + 1, "preview": content[:1500]})
    if obj.get("notes"):
        rs.emit(rid, "build", "info", "What the engineer says it did",
                {"text": str(obj["notes"])[:2000], **meta})
    rs.emit(rid, "build", "done", f"Wrote {len(written)} file(s) into the workspace",
            written)
    return {"files": written, "workspace": str(ws)}


def _verify(rs: RunStore, rid: str, built: dict) -> dict:
    """Cheap, honest checks on what was written. Not a test suite — and says so."""
    rs.set_phase(rid, "verify")
    files = built.get("files") or []
    if not files:
        rs.emit(rid, "verify", "info", "Nothing was built, so there is nothing to verify")
        return {"ok": False}

    ws = Path(built["workspace"])
    problems, checked = [], 0
    for f in files:
        p = ws / f["path"]
        if not p.is_file():
            continue
        src = p.read_text("utf-8", "replace")
        if p.suffix == ".py":
            checked += 1
            try:
                compile(src, f["path"], "exec")
            except SyntaxError as e:
                problems.append({"file": f["path"], "problem": "python syntax error",
                                 "line": e.lineno, "detail": str(e.msg)})
        low = src.lower()
        hit = [m for m in STUB_MARKERS if m in low]
        if hit:
            problems.append({"file": f["path"], "problem": "looks like a stub",
                             "markers": hit})
        if not src.strip():
            problems.append({"file": f["path"], "problem": "file is empty"})

    if problems:
        rs.emit(rid, "verify", "warn", f"{len(problems)} problem(s) in the written files",
                problems, level="warn")
    else:
        rs.emit(rid, "verify", "done",
                f"No syntax errors or stub markers ({checked} python file(s) compiled)")
    rs.emit(rid, "verify", "info", "What this check does NOT cover",
            {"note": "the repo's own test suite and typechecker have not been run — "
                     "that needs the project's toolchain in the workspace"})
    return {"ok": not problems, "problems": problems}


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

        plan = _plan(rs, run_id, cfg, goal, repo, cap, imp)
        if plan.get("blocked"):
            return rs.finish(run_id, "blocked", reason=(
                "Stopped at Plan: " + plan["blocked"]), next_action=(
                "Add an LLM key in Settings and register a model with the "
                "'engineer' role, then start the run again."))
        if plan.get("error"):
            return rs.finish(run_id, "failed", error=plan["error"],
                             reason="The planning model call failed",
                             next_action="Check the model with Test in Settings.")

        built = _build(rs, run_id, cfg, idx, repo, goal, plan["text"], target)
        if built.get("blocked"):
            return rs.finish(run_id, "blocked",
                             reason="Stopped at Build: " + built["blocked"],
                             next_action=f"Make the working tree readable at "
                                         f"/repos/{repo}, then run again.")
        if built.get("error"):
            return rs.finish(run_id, "failed", error=built["error"],
                             reason="Build did not produce usable files",
                             next_action="Inspect the Build events — the raw model "
                                         "output is attached.")

        _verify(rs, run_id, built)

        # Deliberately NOT "done": the work exists but no human has seen it and
        # nothing has been pushed. Saying "done" here is what made the last run
        # look finished when it had barely started.
        rs.emit(run_id, None, "info", "Reached the end of the implemented phases",
                {"built_in": built.get("workspace"),
                 "not_built_yet": "Review (screenshots + diff), Approve, PR"})
        rs.finish(run_id, "incomplete",
                  reason="Built and checked, but Review, Approve and PR are not "
                         "implemented yet — nothing has been shown to a human or pushed.",
                  next_action="Inspect the written files in the Build phase below.")
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
