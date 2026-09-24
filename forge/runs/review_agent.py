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


def _tool(call: dict, root: str, repo_name: str) -> str:
    t = call.get("tool")
    try:
        if t == "read_file":
            p = Path(root) / str(call.get("path", ""))
            if not p.is_file():
                return f"(no such file: {call.get('path')})"
            lines = p.read_text("utf-8", "replace").splitlines()
            s = max(0, int(call.get("start", 1)) - 1)
            e = min(len(lines), int(call.get("end", s + 200)))
            return _clip("\n".join(f"{i+1}: {lines[i]}" for i in range(s, e)), 6000)
        if t == "grep":
            pat = str(call.get("pattern", ""))
            where = str(Path(root) / str(call.get("path", "")))
            r = subprocess.run(["grep", "-rn", "-m", "60", "-I", "--", pat, where],
                               capture_output=True, text=True, timeout=25)
            out = (r.stdout or "").replace(root + "/", "")
            return _clip(out or "(no matches)", 4000)
        if t == "list_dir":
            p = Path(root) / str(call.get("path", ""))
            if not p.is_dir():
                return f"(no such dir: {call.get('path')})"
            return _clip("\n".join(sorted(
                x.name + ("/" if x.is_dir() else "") for x in p.iterdir())), 3000)
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
           max_steps: int = 26, on_event=None, prepared: dict | None = None) -> dict:
    """Run the agentic tool loop with `model`. Returns {findings, summary, verdict, steps}
    or {error}/{why}. Read-only: never writes or pushes.

    `prepared` is a {dir, scratch} from review_exec.prepare_pr — a clean checkout of the
    PR's real head branch (the robust source the caller owns and cleans up). Without it,
    the diff is patched onto the default clone (fails when the base diverged)."""
    ev = on_event or (lambda *a, **k: None)
    if prepared and prepared.get("dir"):
        root, scratch, own = prepared["dir"], None, False
    else:
        prep = REx._prepare(repo_name, diff)
        if prep.get("error"):
            return {"error": prep["error"]}
        root, scratch, own = prep["dir"], prep["scratch"], True
    try:
        convo = (f"PR in `{repo_name}`. Goal: {(goal or '(none)')[:1500]}\n"
                 f"Changed files: {', '.join(changed_files)}\n\n"
                 + (grounding[:8000] + "\n\n" if grounding else "")
                 + f"Unified diff:\n{_clip(diff, 45000)}\n\n"
                 "Begin. Read the changed files, check callers, run the touched tests, "
                 "then give your verdict. Emit ONE JSON action now.")
        for step in range(max_steps):
            text, meta = llm.complete(model, token, SYSTEM, convo, max_tokens=1600)
            if text is None:
                return {"error": meta.get("error", "model call failed")}
            call = _extract_json(text) or {}
            # A verdict is: explicit done:true, OR any response carrying findings (with or
            # without a verdict field) and no further tool call. The earlier check needed
            # both findings AND no-tool, so a model that emitted findings while still
            # nominally "going" produced an empty None verdict — fixed here.
            is_verdict = (call.get("done") or "findings" in call) and not call.get("tool")
            if is_verdict:
                ev("agent", "info", f"done after {step+1} step(s)")
                return {"findings": call.get("findings", []),
                        "summary": call.get("summary", ""),
                        "verdict": call.get("verdict") or (
                            "changes-requested" if call.get("findings") else "pass"),
                        "steps": step + 1, "model": model.get("name")}
            if not call.get("tool"):
                convo += (f"\n\nYOU: {text.strip()[:400]}\n(That was not a valid action. "
                          "Emit ONE JSON: a tool call, or your done verdict with findings.)")
                continue
            ev("agent", "info", f"step {step+1}: {call.get('tool')} {call.get('path') or call.get('pattern') or call.get('paths') or ''}")
            result = _tool(call, root, repo_name)
            convo += f"\n\nYOU: {json.dumps(call)}\nRESULT:\n{result}\n\nNext action (one JSON):"
        # out of steps — force a verdict from everything seen so far
        text, _ = llm.complete(model, token,
                               SYSTEM + "\n\nYou are out of investigation steps. Emit your "
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
