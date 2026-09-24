"""SAST scanner — Semgrep over the change, as a deterministic seed source.

CodeRabbit's edge is bundling linters + SAST with the LLM; this does the same,
self-hosted. It runs Semgrep's curated ruleset over the changed files in a scratch
copy and keeps only findings on lines the diff ADDED, so it flags what the PR
introduces (command injection, eval, weak crypto, unsafe deserialization, …) rather
than pre-existing noise. Best-effort: unavailable (never a false pass) if semgrep or
the clone is missing, or the change will not apply to the base.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import review_exec as REx

_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def _added_lines(diff: str) -> dict:
    """{file: set(new-file line numbers the diff adds)} — so a SAST hit can be
    limited to code this PR introduced."""
    out: dict[str, set] = {}
    cur, ln = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            out.setdefault(cur, set())
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            ln = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out[cur].add(ln)
            ln += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue                      # deleted line: new-file cursor does not advance
        else:
            ln += 1
    return out


def _have_semgrep() -> bool:
    return shutil.which("semgrep") is not None


def scan(repo_name: str | None, diff: str, changed_files: list[str],
         timeout: int = 200) -> dict:
    """Semgrep over the added lines of the change. {available, seeds, note}."""
    if not _have_semgrep():
        return {"available": False, "why": "semgrep not installed", "seeds": []}
    if not repo_name or not diff:
        return {"available": False, "why": "no repo/diff", "seeds": []}
    prep = REx._prepare(repo_name, diff)
    if prep.get("error"):
        return {"available": False, "why": prep["error"], "seeds": []}
    dst, scratch = prep["dir"], prep["scratch"]
    try:
        added = _added_lines(diff)
        targets = [f for f in (changed_files or [])
                   if not REx._is_test(f) and (added.get(f))]
        if not targets:
            return {"available": True, "seeds": [], "note": "no code files with added lines"}
        # Local community rules (offline, deterministic) — `--config auto` needs the
        # registry live and returns nothing in a scratch. Point FORGE_SEMGREP_RULES at
        # a clone of github.com/semgrep/semgrep-rules.
        rules = os.environ.get("FORGE_SEMGREP_RULES", "/config/semgrep-rules")
        cfg = []
        for sub in ("javascript", "typescript", "python", "generic", "dockerfile"):
            d = os.path.join(rules, sub)
            if os.path.isdir(d):
                cfg += ["--config", d]
        if not cfg:
            cfg = ["--config", "auto"]
        cmd = ["semgrep", *cfg, "--json", "--quiet", "--timeout", "25",
               "--max-target-bytes", "1500000", "--metrics", "off", *targets[:40]]
        p = subprocess.run(cmd, cwd=dst, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "SEMGREP_SEND_METRICS": "off"})
        data = json.loads(p.stdout or "{}") if p.stdout.strip().startswith("{") else {}
        seeds, seen = [], set()
        for r in data.get("results", []):
            path = r.get("path")
            line = (r.get("start") or {}).get("line")
            if path not in added or line not in added[path]:
                continue                  # only what the PR introduced
            cid = r["check_id"].lower()
            sev_raw = (r.get("extra") or {}).get("severity")
            if sev_raw == "INFO" or "best-practice" in cid or "best_practice" in cid:
                continue                  # keep security/correctness, drop style nits
            rule = r["check_id"].split("/")[-1].split(".")[-1]
            key = (path, line, rule)
            if key in seen:
                continue
            seen.add(key)
            sev = _SEV.get((r.get("extra") or {}).get("severity"), "low")
            msg = ((r.get("extra") or {}).get("message") or rule).strip()[:220]
            seeds.append({
                "severity": sev, "file": path, "line": line,
                "detail": f"SAST — {rule}: {msg}",
                "angle": "security", "confidence": 75, "verdict": "plausible", "seed": True})
        return {"available": True, "seeds": seeds[:12],
                "note": f"semgrep flagged {len(seeds)} issue(s) on new code"}
    except subprocess.TimeoutExpired:
        return {"available": False, "why": f"semgrep timed out after {timeout}s", "seeds": []}
    except Exception as e:
        return {"available": False, "why": f"{type(e).__name__}: {e}"[:150], "seeds": []}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
