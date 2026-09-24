"""Agentic review — forge drives the tool loop, so ANY model reviews with hands.

The single-shot finder is tool-less: it only sees the diff + grounding we pre-assemble.
This gives the model read-only tools over a scratch clone of the PR — read a file, grep
for callers, list a dir, RUN the touched tests — and loops: the model asks for a tool as
JSON, forge runs it against the clone and feeds the result back, until the model emits
findings. That is what a human reviewer does, and it is the gap with plan-to-pr.

Model-agnostic by design: it uses forge's `llm.complete` and a plain-TEXT tool protocol
(the model prints a JSON action; no provider-specific function-calling), so claude-session,
minimax, glm, or any API model configured in forge can be the reviewer. Tools are
read-only plus test-execution, scoped to the scratch clone — it can look and run, never
write or push.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from ..config import llm
from . import review_exec as REx
from .engine import _extract_json

SYSTEM = (
    "You are a rigorous senior code reviewer WITH TOOLS, reviewing a pull request in a "
    "checked-out clone. Investigate like a human: read each changed file and the code "
    "around it, grep for callers of changed functions, and RUN the touched tests. Reason "
    "briefly, then emit EXACTLY ONE action per turn as a single JSON object on its own "
    "line and nothing after it:\n"
    '  {"tool":"read_file","path":"src/x.ts","start":1,"end":200}\n'
    '  {"tool":"grep","pattern":"functionName","path":"src"}\n'
    '  {"tool":"list_dir","path":"src/lib"}\n'
    '  {"tool":"run_tests","paths":["tests/test_x.py"]}\n'
    "When you have investigated enough (you MUST have read the main changed files and run "
    "the touched tests), emit your verdict:\n"
    '  {"done":true,"summary":"one paragraph","verdict":"pass|pass-with-concerns|'
    'changes-requested","findings":[{"severity":"high|medium|low","file":"path","line":0,'
    '"detail":"the concrete defect, how it triggers, and the fix"}]}\n'
    "Trace concrete inputs. A failing test, an unmet acceptance criterion, a dropped "
    "invariant, PII written to a log, or a new symbol nothing calls is a finding. Prefer "
    "running the tests over guessing. Do not repeat a tool call you already made."
)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… (clipped, {len(s)} chars)"


def _balanced_diff(diff: str, cap: int) -> str:
    """Give EVERY changed file a share of the budget, not the first-N-files-eat-it-all
    that a blind truncation does. On a 256k-char / 55-file PR, a flat clip to 45k showed
    ~12 files and hid 43 — the reason #175 scored 0/14. Here each file's per-file diff
    section is clipped to an equal slice so all files are visible; the agent reads full
    context with read_file for anything clipped."""
    if len(diff) <= cap:
        return diff
    parts = re.split(r"(?=^diff --git )", diff, flags=re.M)
    parts = [p for p in parts if p.strip()]
    if len(parts) <= 1:
        return _clip(diff, cap)
    share = max(1200, cap // len(parts))
    out = []
    for p in parts:
        head = p.split("\n", 1)[0][:200]
        out.append(p if len(p) <= share
                   else p[:share] + f"\n… (file section clipped — read_file for full: {head})")
    joined = "\n".join(out)
    return joined if len(joined) <= cap * 2 else _clip(joined, cap * 2)


_PREFIX = re.compile(r"^/?(home/[^/]+/|work/|repos?/|app/|workspace/|tmp/[^/]+/)+", re.I)


def _resolve(root: str, path: str, want: str = "file") -> Path | None:
    """Map the path a model emitted onto the scratch clone, forgivingly. Models (esp.
    minimax) hallucinate absolute roots like /home/user/<repo>/src/x — and pathlib's
    `root / "/abs"` DISCARDS root, so the read escapes the clone and silently fails
    (this blinded the whole review). Strip hallucinated prefixes and a leading repo
    segment, then fall back to a basename search inside the tree."""
    base_root = Path(root).resolve()
    raw = (path or "").strip().strip('"').strip("'").replace("\\", "/")
    raw = _PREFIX.sub("", raw).lstrip("/")
    cands = [raw]
    if "/" in raw:
        cands.append(raw.split("/", 1)[1])          # maybe a leading repo-name segment
    for c in cands:
        if not c:
            continue
        p = (base_root / c).resolve()
        ok = (p.is_file() if want == "file" else p.is_dir() if want == "dir" else p.exists())
        if ok and str(p).startswith(str(base_root)):
            return p
    name = raw.rsplit("/", 1)[-1]
    if name and want != "dir":
        hits = []
        for q in base_root.rglob(name):
            sq = str(q)
            if "/node_modules/" in sq or "/.git/" in sq:
                continue
            if q.is_file() and str(q.resolve()).startswith(str(base_root)):
                hits.append(q)
            if len(hits) > 40:
                break
        if hits:
            hits.sort(key=lambda q: (0 if str(q).endswith(raw) else 1, len(str(q))))
            return hits[0]
    return None


def _tool(call: dict, root: str, repo_name: str) -> str:
    t = call.get("tool")
    try:
        if t == "read_file":
            rel = call.get("path") or call.get("file") or call.get("filename") or ""
            p = _resolve(root, str(rel), "file")
            if not p:
                return (f"(no such file: {rel!r}. Paths are relative to the repo root; "
                        "call list_dir to see the real layout.)")
            lines = p.read_text("utf-8", "replace").splitlines()
            s = max(0, int(call.get("start", 1) or 1) - 1)
            e = min(len(lines), int(call.get("end", s + 200) or s + 200))
            shown = str(p.relative_to(Path(root).resolve()))
            body = "\n".join(f"{i+1}: {lines[i]}" for i in range(s, e))
            return _clip(f"[{shown}]\n{body}", 6000)
        if t == "grep":
            pat = str(call.get("pattern", ""))
            dirp = _resolve(root, str(call.get("path", "") or ""), "dir") or Path(root)
            r = subprocess.run(["grep", "-rn", "-m", "60", "-I",
                                "--exclude-dir=node_modules", "--exclude-dir=.git",
                                "--", pat, str(dirp)],
                               capture_output=True, text=True, timeout=25)
            out = (r.stdout or "").replace(str(Path(root).resolve()) + "/", "")
            return _clip(out or "(no matches)", 4000)
        if t == "list_dir":
            rel = str(call.get("path", "") or "")
            p = _resolve(root, rel, "dir") or (Path(root) if rel in ("", ".", "/") else None)
            if not p:
                return f"(no such dir: {rel!r}; try list_dir with an empty path for the root)"
            return _clip("\n".join(sorted(
                x.name + ("/" if x.is_dir() else "") for x in p.iterdir()
                if x.name not in ("node_modules", ".git"))), 3000)
        if t == "run_tests":
            paths = [str(x) for x in (call.get("paths") or [])][:12]
            py = [x for x in paths if x.endswith(".py")]
            ts = [x for x in paths if not x.endswith(".py")]
            out = []
            if py:
                rc, o = REx._run_pytest(root, py, 120)
                out.append(f"pytest exit {rc}\n{o[-1600:]}")
            if ts:
                if REx._link_node_modules(repo_name, root):
                    rc, o = REx._run_node(root, ts, 120)
                    out.append(f"tsx --test exit {rc}\n{o[-1600:]}")
                else:
                    out.append("(TS deps not cached — cannot run TS tests)")
            return _clip("\n".join(out) or "(no runnable tests in paths)", 4000)
    except subprocess.TimeoutExpired:
        return "(tool timed out)"
    except Exception as e:
        return f"(tool error: {type(e).__name__}: {e})"
    return f"(unknown tool: {t})"


def review(cfg, model: dict, token: str | None, repo_name: str, diff: str,
           changed_files: list[str], goal: str = "", grounding: str = "",
           max_steps: int = 26, on_event=None, prepared: dict | None = None,
           system_override: str | None = None) -> dict:
    """Run the agentic tool loop with `model`. Returns {findings, summary, verdict, steps}
    or {error}/{why}. Read-only: never writes or pushes.

    `prepared` is a {dir, scratch} from review_exec.prepare_pr — a clean checkout of the
    PR's real head branch (the robust source the caller owns and cleans up). Without it,
    the diff is patched onto the default clone (fails when the base diverged).
    `system_override` swaps the reviewer persona (used by the adversarial BREAK pass)."""
    ev = on_event or (lambda *a, **k: None)
    sys_prompt = system_override or SYSTEM
    if prepared and prepared.get("dir"):
        root, scratch, own = prepared["dir"], None, False
    else:
        prep = REx._prepare(repo_name, diff)
        if prep.get("error"):
            return {"error": prep["error"]}
        root, scratch, own = prep["dir"], prep["scratch"], True
    # Scale the investigation budget to PR size — a 55-file PR cannot be reviewed in the
    # 26 steps that suit a 3-file one (the other half of why #175 scored 0/14).
    nfiles = len([f for f in changed_files if f])
    max_steps = max(max_steps, min(46, 22 + nfiles // 2))
    try:
        # Seed the real repo layout so the model emits paths that actually resolve
        # (models otherwise guess /home/user/<repo>/… which escapes the clone).
        try:
            top = sorted(x.name + ("/" if x.is_dir() else "") for x in Path(root).iterdir()
                         if x.name not in ("node_modules", ".git"))
            tree = "Repo root contents (paths you pass are relative to THIS root):\n" + \
                   ", ".join(top[:60]) + "\n\n"
        except Exception:
            tree = ""
        flist = ", ".join(changed_files)
        convo = (f"PR in `{repo_name}`. Goal: {(goal or '(none)')[:1500]}\n"
                 f"Changed files ({nfiles}) — you MUST account for every one of them: {flist}\n\n"
                 + tree
                 + (grounding[:8000] + "\n\n" if grounding else "")
                 + f"Unified diff (per-file; large files are clipped — use read_file to see "
                   f"full context, do NOT skip a file because its diff is clipped):\n"
                 + f"{_balanced_diff(diff, 60000)}\n\n"
                 "Begin. Work through ALL changed files: read each one and the code around it, "
                 "check callers, run the touched tests, then give your verdict covering the "
                 "whole PR. Emit ONE JSON action now.")
        # Investigation floor: a model must not conclude on a big PR after skimming a
        # handful of files (claude tried to finish #175 in 6 steps over 55 files). Require
        # a minimum number of distinct files read before a verdict is accepted; push back
        # a bounded number of times so it can't be gamed and can't loop forever.
        floor = min(nfiles, 10) if nfiles > 6 else max(1, nfiles - 1)
        read_paths: set[str] = set()
        pushbacks = 0
        for step in range(max_steps):
            text, meta = llm.complete(model, token, sys_prompt, convo, max_tokens=1600)
            if text is None:
                return {"error": meta.get("error", "model call failed")}
            call = _extract_json(text) or {}
            # A verdict is: explicit done:true, OR any response carrying findings (with or
            # without a verdict field) and no further tool call. The earlier check needed
            # both findings AND no-tool, so a model that emitted findings while still
            # nominally "going" produced an empty None verdict — fixed here.
            is_verdict = (call.get("done") or "findings" in call) and not call.get("tool")
            if is_verdict:
                if len(read_paths) < floor and pushbacks < 3 and step < max_steps - 2:
                    pushbacks += 1
                    unread = [f for f in changed_files
                              if not any(f.endswith(rp) or rp.endswith(f.rsplit("/", 1)[-1])
                                         for rp in read_paths)][:8]
                    ev("agent", "info",
                       f"floor: read {len(read_paths)}/{floor} files — pushing to investigate more")
                    convo += (f"\n\nYOU tried to conclude, but you have only examined "
                              f"{len(read_paths)} of {nfiles} changed files. Do NOT conclude yet. "
                              f"Read these still-unexamined changed files and check their behaviour "
                              f"first: {', '.join(unread) or '(the remaining changed files)'}. "
                              "Emit ONE tool call now.")
                    continue
                ev("agent", "info", f"done after {step+1} step(s)")
                return {"findings": call.get("findings", []),
                        "summary": call.get("summary", ""),
                        "verdict": call.get("verdict") or (
                            "changes-requested" if call.get("findings") else "pass"),
                        "steps": step + 1, "model": model.get("name")}
            if call.get("tool") == "read_file":
                rp = str(call.get("path") or call.get("file") or "").strip()
                if rp:
                    read_paths.add(rp)
            if not call.get("tool"):
                convo += (f"\n\nYOU: {text.strip()[:400]}\n(That was not a valid action. "
                          "Emit ONE JSON: a tool call, or your done verdict with findings.)")
                continue
            ev("agent", "info", f"step {step+1}: {call.get('tool')} {call.get('path') or call.get('pattern') or call.get('paths') or ''}")
            result = _tool(call, root, repo_name)
            convo += f"\n\nYOU: {json.dumps(call)}\nRESULT:\n{result}\n\nNext action (one JSON):"
        # out of steps — force a verdict from everything seen so far
        text, _ = llm.complete(model, token,
                               sys_prompt + "\n\nYou are out of investigation steps. Emit your "
                               "done verdict JSON NOW based on what you have seen.",
                               convo, max_tokens=1600)
        j = _extract_json(text or "") or {}
        return {"findings": j.get("findings", []), "summary": j.get("summary", ""),
                "verdict": j.get("verdict") or (
                    "changes-requested" if j.get("findings") else "pass"),
                "steps": max_steps, "model": model.get("name")}
    finally:
        if own and scratch:
            shutil.rmtree(scratch, ignore_errors=True)
