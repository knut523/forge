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

from ..config.store import GITHUB_WRITE, ConfigStore
from ..config import llm
from ..config import providers
from . import ship
from .memory import (MemoryStore, harvest_conventions, remember_verify_problems)
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


def _capture(rs: RunStore, rid: str, idx: IndexStore, repo: str,
             mem: MemoryStore, root=None) -> dict:
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

    # The repo's own instructions to contributors outrank anything we infer. Stamp them with the
    # head SHA so a convention that changes in a later commit supersedes rather than overwrites.
    names = harvest_conventions(mem, repo, root, sha=ov.get("head_sha"))
    if names:
        rs.emit(rid, "capture", "finding",
                f"Read {len(names)} convention(s) from the repo's own docs", names)
    known = mem.list(repo)
    if known:
        rs.emit(rid, "capture", "metric",
                f"{len(known)} thing(s) already known about this repo",
                [{"kind": m["kind"], "what": m["title"]} for m in known[:15]])

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
          cap: dict, imp: dict, recall: str = "") -> dict:
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
        f"GOAL\n{goal}\n\nGROUNDED FACTS ABOUT THE REPOSITORY\n{facts}"
        + (f"\n\n{recall}" if recall else ""))
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


def _recover_files(text: str) -> list[dict]:
    """Salvage files from a build that ignored the JSON contract and emitted fenced
    code blocks. A path is taken from the fence info string (```ts src/x.ts) or the
    non-empty line just before the fence (a heading like **src/x.ts**)."""
    path_re = re.compile(r"[\w./-]*[\w-]/[\w./-]*\.[A-Za-z0-9]+|[\w-]+\.[A-Za-z0-9]+")
    fence_re = re.compile(
        r"(?:^|\n)[ \t]*(?P<pre>[^\n]*)\n[ \t]*```(?P<info>[^\n]*)\n(?P<body>.*?)\n[ \t]*```",
        re.S)
    out = {}
    for m in fence_re.finditer(text):
        body = m.group("body")
        if not body.strip():
            continue
        path = None
        for cand in (m.group("info"), m.group("pre")):
            pm = path_re.search(cand or "")
            if pm and ("/" in pm.group(0) or "." in pm.group(0)):
                path = pm.group(0).strip("*`# ")
                break
        if path:
            out[path] = body          # last block for a path wins
    return [{"path": p, "content": c} for p, c in out.items()]


def _sibling_test(idx: IndexStore, repo: str, root: Path,
                  wanted: list[str]) -> tuple[str | None, str | None]:
    """An existing test in this repo, to show the model the house test convention —
    runner, imports, assertion API. A generated test that guesses the wrong framework
    (Jest-style `describe/it/expect` where the repo runs `node:test`, say) typechecks
    red even when the code under test is perfect. Showing a real neighbour is what a
    human does instead of guessing, and it costs one short read."""
    langs = {os.path.splitext(w)[1] for w in wanted}
    if {".ts", ".tsx", ".js", ".jsx"} & langs:
        pats = ["%.test.ts", "%.test.tsx", "%.test.js", "%.spec.ts"]
    elif ".py" in langs:
        pats = ["%\\_test.py", "test\\_%.py"]
    else:
        return None, None
    prefer_dir = os.path.dirname(wanted[0]) if wanted else ""
    rows = []
    for pat in pats:
        rows += idx.db.execute(
            "SELECT f.path FROM files f JOIN repos r ON r.id=f.repo_id"
            " WHERE r.name=? AND f.path LIKE ? ESCAPE '\\' ORDER BY LENGTH(f.path) LIMIT 25",
            (repo, pat)).fetchall()
    cands = [r["path"] for r in rows if r["path"] not in wanted]
    if not cands:
        return None, None
    cands.sort(key=lambda p: (0 if os.path.dirname(p) == prefer_dir else 1, len(p)))
    for rel in cands:
        p = root / rel
        if p.is_file():
            try:
                return rel, "\n".join(p.read_text("utf-8", "replace").splitlines()[:45])
            except OSError:
                continue
    return None, None


def _build(rs: RunStore, rid: str, cfg: ConfigStore, idx: IndexStore, repo: str,
           goal: str, plan_text: str, target: str | None, mem: MemoryStore,
           feedback: str | None = None, sibling_ctx: str | None = None) -> dict:
    """Write the change into an isolated workspace. Never touches the source tree.
    `feedback` (auto-revise round) carries the previous attempt + review findings to
    fix. `sibling_ctx` carries files an earlier PR in the same feature adds, so a
    dependent item can build on them before they are merged."""
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
    # If a test is in scope, show the model a real sibling test so it matches the
    # repo's runner and assertion style instead of guessing a framework.
    sib_path, sib_head = None, None
    if re.search(r"\btest", f"{goal or ''} {plan_text or ''}", re.I):
        sib_path, sib_head = _sibling_test(idx, repo, root, wanted)
    rs.emit(rid, "build", "step", f"Building with {model['name']}",
            {"files_given_to_the_model": [f["path"] for f in seen] or "none",
             "test_convention_from": sib_path or "none",
             "workspace": f"/work/{rid}"})

    # Build sees only the memories that pin to or mention the files it is actually about — the plan
    # already chose those files, so this is the precise, scoped recall NEXT step 5 asks for (Plan
    # got the full set). A gotcha about a file this change never opens is noise here.
    recall = mem.for_prompt(repo, touched=[f["path"] for f in seen])
    ctx = "\n\n".join(f"--- FILE {f['path']} ---\n{f['content']}" for f in seen)
    text, meta = llm.complete(
        model, token, BUILD_SYSTEM,
        f"GOAL\n{goal}\n\nPLAN\n{plan_text}"
        + (f"\n\n{recall}" if recall else "")
        + (f"\n\nSIBLING FILES — earlier PRs in this feature add these; build ON them "
           f"(they exist once merged), do not recreate them:\n{sibling_ctx}"
           if sibling_ctx else "")
        + (f"\n\nTEST CONVENTION — this repo already has tests. If you write a test, "
           f"match this neighbour's test runner, imports and assertion API EXACTLY; do "
           f"NOT introduce a different framework:\n--- {sib_path} ---\n{sib_head}"
           if sib_head else "")
        + f"\n\nCURRENT FILES\n{ctx or '(none supplied)'}"
        + (f"\n\nREVISION — fix the review findings, keep everything else:\n{feedback}"
           if feedback else ""),
        max_tokens=24000)
    if text is None:
        rs.emit(rid, "build", "error", "The model call failed", meta, level="error")
        return {"error": meta.get("error")}

    obj = _extract_json(text) or {}
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        # The model sometimes ignores the JSON contract on big builds and emits
        # fenced or bare code blocks instead. Recover them rather than failing the
        # whole run for a format slip.
        recovered = _recover_files(text)
        if recovered:
            files = recovered
            rs.emit(rid, "build", "info",
                    f"Recovered {len(files)} file(s) from non-JSON output")
        else:
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


def _run_pytests(rs: RunStore, rid: str, ws: Path, root, test_files: list[str]) -> None:
    """Best-effort: actually run the python tests that were written. Imports resolve
    from the workspace first (the new/changed files) then the real working tree (the
    rest of the repo). Missing project deps mean a test *cannot* run here — reported
    as such, never as a failure."""
    import os as _os
    import subprocess
    env = dict(_os.environ)
    parts = [str(ws)] + ([str(root)] if root else [])
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = ":".join(parts)
    try:
        r = subprocess.run(
            ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
             *[str(ws / t) for t in test_files]],
            cwd=str(ws), env=env, capture_output=True, text=True, timeout=150)
    except Exception as e:
        rs.emit(rid, "verify", "info", f"could not run tests: {type(e).__name__}",
                {"error": str(e)[:200]})
        return
    tail = "\n".join((r.stdout or r.stderr).strip().splitlines()[-10:])
    if r.returncode == 0:
        rs.emit(rid, "verify", "done", f"Tests passed ({len(test_files)} file(s))",
                {"output": tail})
    elif r.returncode == 5:
        rs.emit(rid, "verify", "info", "pytest collected no tests", {"output": tail})
    else:
        blob = (r.stdout + r.stderr).lower()
        could_not = ("modulenotfounderror" in blob or "importerror" in blob) \
            and "assert" not in blob
        rs.emit(rid, "verify", "warn",
                ("tests could not run here — likely missing project deps in the forge "
                 "container (not a real failure)" if could_not
                 else "tests FAILED — see output"),
                {"output": tail, "returncode": r.returncode, "could_not_run": could_not},
                level="warn")


def _verify(rs: RunStore, rid: str, built: dict, root=None) -> dict:
    """Cheap, honest checks on what was written, and a best-effort run of any tests
    it wrote. Still not a full CI — and says so."""
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

    # actually run any tests that were written (best-effort, honest about deps)
    test_files = [f["path"] for f in files
                  if f["path"].endswith(".py")
                  and Path(f["path"]).name.startswith(("test_", "test"))
                  and "test" in Path(f["path"]).name]
    if test_files:
        _run_pytests(rs, rid, ws, root, test_files)

    rs.emit(rid, "verify", "info", "What this check does NOT cover",
            {"note": "the typechecker and JS/TS test suites are not run here, and "
                     "python tests needing uninstalled project deps cannot run in the "
                     "forge container — treat those as unverified, not as passing"})
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


def _revise_feedback(diffs: list[dict], findings: list[dict]) -> str:
    """The prompt the builder gets on an auto-revise round: its own previous diff
    plus the exact findings it must fix."""
    diff = "\n".join(f"--- {d.get('path')}\n{d.get('diff', '')}" for d in diffs)
    if len(diff) > 20000:
        diff = diff[:20000] + "\n… (truncated)"
    issues = "\n".join(
        f"- [{f.get('severity', '?')}] {f.get('file', '')}"
        f"{(':' + str(f['line'])) if f.get('line') else ''} — {f.get('detail', '')}"
        for f in findings)
    return (f"Your previous attempt produced this diff:\n{diff}\n\n"
            f"A review found these issues you MUST fix:\n{issues}\n\n"
            f"Return corrected FULL file contents that resolve every issue above "
            f"without reintroducing them and without changing unrelated behaviour.")


def execute(run_id: str, index_db: str) -> None:
    """Run the pipeline. Never raises — a crash is recorded as a failed run."""
    rs = RunStore()
    cfg = ConfigStore()
    mem = MemoryStore()
    idx = None
    try:
        run = rs.get(run_id)
        if run is None:
            return
        repo, goal, target = run["repo"], run["goal"], run["target"]
        _st0 = run.get("state")
        _st0 = json.loads(_st0) if isinstance(_st0, str) else (_st0 or {})
        sibling_ctx = _st0.get("sibling_ctx")   # files an earlier PR in this feature adds
        rs.emit(run_id, None, "info", "Run started",
                {"repo": repo, "goal": goal, "target": target})
        idx = IndexStore(index_db, readonly=True)
        root = _repo_root(idx, repo)

        cap = _capture(rs, run_id, idx, repo, mem, root)
        imp = _impact(rs, run_id, idx, repo, target)

        # Plan gets the full ranked set — it has not chosen files yet. Build later gets only the
        # memories that touch the files the plan settled on: same knowledge, scoped where it can be.
        recall_plan = mem.for_prompt(repo)

        plan = _plan(rs, run_id, cfg, goal, repo, cap, imp, recall_plan)
        if plan.get("blocked"):
            return rs.finish(run_id, "blocked", reason=(
                "Stopped at Plan: " + plan["blocked"]), next_action=(
                "Add an LLM key in Settings and register a model with the "
                "'engineer' role, then start the run again."))
        if plan.get("error"):
            return rs.finish(run_id, "failed", error=plan["error"],
                             reason="The planning model call failed",
                             next_action="Check the model with Test in Settings.")

        origin = idx.db.execute("SELECT origin FROM repos WHERE name=?",
                                (repo,)).fetchone()
        origin = origin["origin"] if origin else None
        import forge.runs.pr_review as _PRR

        # Build → verify → review, looping to AUTO-REVISE when the review finds
        # blocking issues, so a human approves refined work rather than a first
        # draft. Bounded by AUTO_REVISE_ROUNDS to cap cost; whatever is unresolved
        # after that is surfaced at the gate for the human to decide.
        AUTO_REVISE_ROUNDS = 10   # keep revising until the review is clean …
        feedback, built, reviewed, review_out, rounds = None, {}, {}, {}, 0
        prev_blocking = None      # … or a round stops reducing findings (no progress)
        while True:
            built = _build(rs, run_id, cfg, idx, repo, goal, plan["text"], target,
                           mem, feedback=feedback, sibling_ctx=sibling_ctx)
            if built.get("blocked"):
                return rs.finish(run_id, "blocked",
                                 reason="Stopped at Build: " + built["blocked"],
                                 next_action=f"Make the working tree readable at "
                                             f"/repos/{repo}, then run again.")
            if built.get("error"):
                return rs.finish(run_id, "failed", error=built["error"],
                                 reason="Build did not produce usable files",
                                 next_action="Inspect the Build events — the raw "
                                             "model output is attached.")
            verified = _verify(rs, run_id, built, root)
            if verified.get("problems"):
                remember_verify_problems(mem, repo, run_id, verified["problems"],
                                         sha=cap.get("overview", {}).get("head_sha"))
            reviewed = ship.review(rs, run_id, built, verified, root, imp)
            if not reviewed.get("ok"):
                return rs.finish(run_id, "incomplete",
                                 reason="Nothing was produced to review.",
                                 next_action="Check the Build phase output.")
            review_out = {}
            try:
                rs.set_phase(run_id, "review")
                cr = imp.get("cross_repo") or []
                note = ("reaches " + ", ".join(sorted({c["repo"] for c in cr}))
                        if cr else "no cross-repo impact detected")
                review_out = _PRR.review_built(
                    cfg, repo, goal, reviewed["diffs"], note,
                    on_event=lambda ph, k, t, dt=None: rs.emit(run_id, "review", k, t, dt),
                    sibling_ctx=sibling_ctx)
            except Exception as e:
                rs.emit(run_id, "review", "warn",
                        f"review pass skipped: {type(e).__name__}", {"error": str(e)[:200]})
            findings = review_out.get("findings") or []
            blocking = [f for f in findings
                        if str(f.get("severity", "")).lower() in ("high", "medium")
                        or (f.get("confidence") or 0) >= 60]
            rounds += 1
            review_out["rounds"] = rounds
            rs.emit(run_id, "review", "done",
                    f"Review round {rounds}: {len(findings)} finding(s), "
                    f"{len(blocking)} blocking · verdict {review_out.get('verdict', '—')}",
                    review_out)
            stalled = prev_blocking is not None and len(blocking) >= prev_blocking
            if not blocking or rounds > AUTO_REVISE_ROUNDS or stalled:
                if blocking:
                    why = ("it stopped reducing findings" if stalled
                           else f"after {rounds - 1} auto-revise round(s)")
                    rs.emit(run_id, "review", "warn",
                            f"{len(blocking)} finding(s) remain — {why} — left for "
                            f"your decision", level="warn")
                break
            prev_blocking = len(blocking)
            rs.emit(run_id, "review", "step",
                    f"Auto-revising to fix {len(blocking)} finding(s) — round {rounds + 1}")
            feedback = _revise_feedback(reviewed["diffs"], blocking)

        review_out["auto_revised"] = rounds - 1
        rs.set_state(run_id, {"built": built, "reviewed": reviewed,
                              "origin": origin, "review": review_out})

        # The run STOPS here. The PR phase is not reachable by falling through;
        # it runs only from an explicit approve call, which is what makes
        # "nothing reaches GitHub without a human" structural rather than a rule.
        rs.set_phase(run_id, "approve")
        rs.emit(run_id, "approve", "info", "Waiting for your decision",
                {"files": len(reviewed["diffs"]),
                 "additions": reviewed["additions"],
                 "deletions": reviewed["deletions"],
                 "review_findings": len(review_out.get("findings") or []),
                 "review_verdict": review_out.get("verdict"),
                 "auto_revised": rounds - 1,
                 "github_target": origin or "none — this repo has no GitHub origin"})
        rs.finish(run_id, "awaiting_approval",
                  reason=(f"Built, reviewed" + (f" and auto-revised {rounds - 1}×"
                          if rounds > 1 else "") + ". Nothing has been pushed."),
                  next_action="Review the diff below, then Approve to open a PR, "
                              "or Reject to discard it.")
        # An epic item that reaches its gate is "built enough" for its dependents to
        # start — so the whole feature builds up front and waits for you in a batch,
        # instead of one-approve-at-a-time.
        if run.get("parent_id"):
            try:
                from . import epic as _EP
                _EP.advance(run["parent_id"], index_db)
            except Exception:
                pass
    except Exception as e:
        rs.emit(run_id, None, "error", f"Run failed: {type(e).__name__}",
                {"error": str(e)[:400], "trace": traceback.format_exc()[-1200:]},
                level="error")
        rs.finish(run_id, "failed", f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if idx is not None:
            idx.close()
        mem.close()
        cfg.close()
        # If an epic child ended in a terminal non-gate state, tell the epic so it
        # surfaces the failure instead of hanging on a stuck item.
        try:
            r2 = rs.get(run_id)
            if r2 and r2.get("parent_id") and r2.get("status") in (
                    "failed", "blocked", "incomplete"):
                from . import epic as _EP
                _EP.on_child_finished(run_id, index_db)
        except Exception:
            pass
        rs.close()


def start(run_id: str, index_db: str) -> None:
    threading.Thread(target=execute, args=(run_id, index_db), daemon=True).start()


# ─── after the human decides ────────────────────────────────────────────────

def ship_it(run_id: str) -> None:
    """Open the PR. Reached only from an approve call — never by falling through.

    This is the one place the WRITE token is read, and it is read after the
    approval is already recorded.
    """
    rs = RunStore()
    cfg = ConfigStore()
    try:
        run = rs.get(run_id)
        state = rs.get_state(run_id)
        built, reviewed = state.get("built"), state.get("reviewed")
        origin = state.get("origin")
        if not built or not reviewed:
            return rs.finish(run_id, "failed",
                             reason="Approved, but the reviewed change is gone.",
                             next_action="Start the run again.")
        if not origin:
            return rs.finish(run_id, "blocked",
                             reason=f"Approved, but {run['repo']!r} has no GitHub "
                                    f"origin recorded, so there is nowhere to open "
                                    f"a PR.",
                             next_action="Index a repo cloned from GitHub, then "
                                         "run again.")
        token = cfg.get_secret(GITHUB_WRITE)
        if not token:
            return rs.finish(run_id, "blocked",
                             reason="Approved, but no GitHub WRITE token is set. "
                                    "Nothing was pushed.",
                             next_action="Add the write token in Settings, then "
                                         "approve again.")
        # A write token that cannot write fails halfway through, leaving a
        # branch and no PR. Check before touching anything.
        v = providers.verify_github(token, expect="write")
        if not v.get("ok"):
            return rs.finish(run_id, "blocked",
                             reason=f"The write token was rejected: {v.get('detail')}",
                             next_action="Replace it in Settings and approve again.")
        for w in v.get("warnings", []):
            rs.emit(run_id, "pr", "warn", w, level="warn")

        try:
            pr = ship.open_pr(rs, run_id, run, built, reviewed, origin, token)
        except Exception as e:
            rs.emit(run_id, "pr", "error", "Opening the PR failed",
                    {"error": str(e)[:600]}, level="error")
            return rs.finish(run_id, "failed", error=str(e)[:200],
                             reason="The PR was not opened.",
                             next_action="Check the PR phase events.")
        rs.set_pr(run_id, pr["url"])
        rs.finish(run_id, "complete",
                  reason=f"PR #{pr['number']} opened against {pr['base']}, ready "
                         f"for review.",
                  next_action=pr["url"])
        parent = run.get("parent_id")
    finally:
        cfg.close()
        rs.close()
    if parent:                    # an item of a feature — let the epic advance
        from . import epic
        epic.on_child_finished(run_id, os.environ.get("FORGE_DB", "/data/forge.db"))


def approve(run_id: str) -> None:
    threading.Thread(target=ship_it, args=(run_id,), daemon=True).start()
