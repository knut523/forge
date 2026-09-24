"""Grounding for the PR review — the context a careful human reviewer has and a
diff-only model does not.

Everything here is deterministic, read-only, and best-effort (a failure returns
empty, never sinks a review). It turns the local clone, the code index and repo
memory into the facts forge's review used to skip:

  * the repo's own conventions (AGENTS.md), already in repo memory;
  * the PR's acceptance criteria (from the body and any linked plan doc);
  * whether new code is actually REACHED (grep the clone + the diff for callers);
  * whether new tests actually RUN in CI (package.json scripts + workflows);
  * the enclosing code around each hunk, not just the hunk.

The reachability and CI-wiring checks are the important ones: they are the
classes forge shipped past on olaf #190 ("there is no report", "the test never
runs in CI"), and they are decidable without a model, so they are emitted as
seed findings the review must carry rather than left to the LLM to notice.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

WORK = os.environ.get("FORGE_WORK", "/work")
_DOCS_REPO = os.environ.get("FORGE_DOCS_REPO", "olaf-docs")


def clone_dir(repo_name: str | None) -> Path | None:
    if not repo_name:
        return None
    d = Path(WORK) / repo_name
    return d if d.is_dir() else None


def _read(p: Path, cap: int = 20000) -> str:
    try:
        return p.read_text("utf-8", "replace")[:cap]
    except Exception:
        return ""


# ---------------------------------------------------------------- conventions
def conventions(repo_name: str | None) -> str:
    """The repo's own rules — AGENTS.md / CLAUDE.md / CONTRIBUTING, verbatim and
    capped. Repo memory harvests these too, but the review wants the source text,
    not a summary, so it can quote a specific rule."""
    d = clone_dir(repo_name)
    if not d:
        return ""
    out = []
    for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
        t = _read(d / name, cap=8000)
        if t:
            out.append(f"### {name}\n{t}")
    return "\n\n".join(out)


# ------------------------------------------------------------ acceptance criteria
_ACC_HEAD = re.compile(
    r"^#{1,6}\s*(acceptance|akzeptanz|done when|definition of done|abnahme"
    r"|acceptance criteria|akzeptanzkriterien)\b", re.I)
_PLAN_REF = re.compile(r"plans?/(\d{2,4})[-/]?([a-z0-9-]*)", re.I)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)")


def _bullets_after_heading(md: str) -> list[str]:
    """Bullets that sit under an acceptance-style heading, until the next heading."""
    lines = md.splitlines()
    out, grabbing = [], False
    for ln in lines:
        if _ACC_HEAD.match(ln):
            grabbing = True
            continue
        if grabbing and re.match(r"^#{1,6}\s", ln):
            break
        if grabbing:
            m = _BULLET.match(ln)
            if m:
                out.append(m.group(1).strip())
    return out


def acceptance_criteria(pr_body: str, repo_name: str | None) -> dict:
    """Criteria the diff must satisfy, drawn from the PR body first and then any
    plan doc it cites in olaf-docs. Returns {criteria, plan_ref, source}."""
    body = pr_body or ""
    crit = _bullets_after_heading(body)
    plan_ref = None
    m = _PLAN_REF.search(body)
    if m:
        num = m.group(1)
        docs = clone_dir(_DOCS_REPO)
        if docs:
            for cand in sorted((docs / "plans").glob(f"{num}*")) if (docs / "plans").is_dir() else []:
                txt = _read(cand)
                if txt:
                    plan_ref = cand.name
                    crit += _bullets_after_heading(txt)
                    break
    # de-dupe, keep order, cap
    seen, ordered = set(), []
    for c in crit:
        k = c.lower()[:80]
        if k not in seen:
            seen.add(k)
            ordered.append(c)
    return {"criteria": ordered[:20], "plan_ref": plan_ref,
            "source": ("body+plan" if plan_ref else "body") if ordered else "none"}


# ----------------------------------------------------------------- reachability
# New top-level definitions a diff introduces, per language. We only need the
# name; the point is "does anything call it", not a full parse.
_ADDED = [
    re.compile(r"^\+.*\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\+.*\bexport\s+const\s+([A-Za-z_$][\w$]*)\s*="),
    re.compile(r"^\+.*\bexport\s+class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\+\s*def\s+([A-Za-z_][\w]*)\s*\("),
    re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\("),
]
_DUNDER = re.compile(r"^(test_|__|_)")


def _added_symbols(diff: str) -> list[dict]:
    out, cur_file = [], None
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            cur_file = ln[6:].strip()
            continue
        for rx in _ADDED:
            m = rx.match(ln)
            if m:
                name = m.group(1)
                if name and not _DUNDER.match(name) and len(name) > 2:
                    out.append({"name": name, "file": cur_file})
    # de-dupe by name
    seen, uniq = set(), []
    for s in out:
        if s["name"] not in seen:
            seen.add(s["name"])
            uniq.append(s)
    return uniq


def _grep_count(repo_dir: Path, name: str) -> list[str]:
    """Files in the clone that mention `name` (word-boundary). git grep first,
    plain grep as a fallback so an odd checkout still answers."""
    for cmd in (["git", "-C", str(repo_dir), "grep", "-lIw", name],
                ["grep", "-rlIw", "--", name, str(repo_dir)]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if p.returncode in (0, 1):
                return [x for x in p.stdout.splitlines() if x.strip()]
        except Exception:
            continue
    return []


def _is_test_path(path: str) -> bool:
    p = path.lower()
    return (".test." in p or ".spec." in p or "_test." in p
            or "/tests/" in p or "/test/" in p or p.endswith("_test.py")
            or "/__tests__/" in p)


def reachability(diff: str, repo_name: str | None, changed_files: list[str]) -> list[dict]:
    """Seed findings for exported/defined-but-never-called new symbols.

    A symbol is an orphan when the only places that mention it are its own
    definition file and test files — in the clone AND in the diff. That is the
    'there is no report' shape: a function and its test land, nothing calls it.
    A new function whose caller is added in the same PR is correctly NOT flagged.
    """
    d = clone_dir(repo_name)
    if not d:
        return []
    changed = set(changed_files or [])
    findings = []
    for sym in _added_symbols(diff)[:40]:
        name, deffile = sym["name"], sym["file"] or ""
        if _is_test_path(deffile):
            continue
        hits = _grep_count(d, name)
        # references in the clone outside the symbol's own (base) file and tests
        base_refs = [h for h in hits
                     if not _is_test_path(h) and not h.endswith(deffile.split("/")[-1])]
        # references elsewhere in THIS diff (a caller the PR itself adds)
        diff_callers = [f for f in changed
                        if f != deffile and not _is_test_path(f)
                        and _mentions_in_diff(diff, f, name)]
        # used WITHIN its own module (an internal helper is not dead code, even if the
        # base clone does not have the new file yet). Only a symbol used nowhere — not
        # internally, not by a sibling changed file, not in the base — is an orphan.
        intra = _mentions_in_diff(diff, deffile, name)
        if not base_refs and not diff_callers and not intra:
            findings.append({
                "severity": "high",
                "file": deffile,
                "detail": (f"`{name}` is defined here but nothing calls it — not within "
                           f"its own module, not by another file in this PR, and not in "
                           f"the clone (git grep finds only its own file/tests). If an "
                           f"acceptance criterion says this produces output, it cannot be "
                           f"met: there is no caller. Add the wiring (route/script/caller) "
                           f"or it is dead code."),
                "angle": "reachability",
                "confidence": 80,
                "verdict": "confirmed",
                "seed": True,
            })
    return findings


def _mentions_in_diff(diff: str, file: str, name: str) -> bool:
    """Does the diff's added content for `file` reference `name` (not just define it)."""
    want = f"+++ b/{file}"
    in_file, rx = False, re.compile(r"\b" + re.escape(name) + r"\b")
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            in_file = (ln.strip() == want)
            continue
        if in_file and ln.startswith("+") and not ln.startswith("+++"):
            # ignore the definition line itself
            if rx.search(ln) and not re.search(
                    r"\b(def|function|class|const)\s+" + re.escape(name), ln):
                return True
    return False


# ------------------------------------------------------------------- CI wiring
def ci_wiring(diff: str, repo_name: str | None, changed_files: list[str]) -> list[dict]:
    """Seed findings for new test files no CI-run script executes.

    A test that exists and is never run by the thing that gates merges passes
    locally and never again — how olaf #178 reached main. We read the base
    clone's package.json scripts and any script the diff itself adds.
    """
    d = clone_dir(repo_name)
    if not d:
        return []
    new_tests = [f for f in (changed_files or [])
                 if _is_test_path(f) and _added_in_diff(diff, f)]
    if not new_tests:
        return []
    scripts = _all_test_scripts(d, diff)
    if not scripts:
        return [{
            "severity": "medium", "file": nt,
            "detail": (f"`{nt}` is a new test, but no runnable test script was found "
                       f"in package.json — so CI cannot run it. Add it to a script CI "
                       f"invokes, or it passes once locally and never gates a merge."),
            "angle": "ci-wiring", "confidence": 70, "verdict": "plausible", "seed": True}
            for nt in new_tests[:8]]
    wf = _workflow_scripts(d)
    findings = []
    for nt in new_tests[:12]:
        covering = _covering_script(nt, scripts)
        if not covering:
            findings.append({
                "severity": "medium", "file": nt,
                "detail": (f"`{nt}` is not reached by any test script in package.json "
                           f"({', '.join(sorted(scripts)[:6])}…). It runs locally and is "
                           f"never executed by CI. Add it to an existing suite (one line)."),
                "angle": "ci-wiring", "confidence": 65, "verdict": "plausible", "seed": True})
        elif wf and covering not in wf:
            # The script exists but no GitHub workflow invokes it — this is how a test
            # reaches main while never being run by the thing that gates merges.
            findings.append({
                "severity": "medium", "file": nt,
                "detail": (f"`{nt}` runs under the `{covering}` script, but no GitHub "
                           f"workflow invokes `{covering}` (workflows run: "
                           f"{', '.join(sorted(wf)[:6]) or 'none'}). It passes locally and "
                           f"CI never runs it — add `{covering}` to the PR/CI workflow."),
                "angle": "ci-wiring", "confidence": 70, "verdict": "plausible", "seed": True})
    return findings


def _added_in_diff(diff: str, file: str) -> bool:
    return f"+++ b/{file}" in diff


def _all_test_scripts(repo_dir: Path, diff: str) -> dict:
    """script-name -> command, for scripts that look like they run tests. Base
    package.json plus any scripts the diff adds."""
    scripts = {}
    try:
        pkg = json.loads(_read(repo_dir / "package.json", cap=40000) or "{}")
        for k, v in (pkg.get("scripts") or {}).items():
            if _looks_like_test(k, v):
                scripts[k] = v
    except Exception:
        pass
    # scripts added in the diff (a PR that wires its own test)
    for ln in diff.splitlines():
        m = re.match(r'^\+\s*"([\w:.-]+)"\s*:\s*"(.+?)"\s*,?\s*$', ln)
        if m and _looks_like_test(m.group(1), m.group(2)):
            scripts[m.group(1)] = m.group(2)
    return scripts


_RUNNERS = ("jest", "vitest", "node --test", "node:test", "mocha", "pytest",
            "tsx --test", "playwright test", "ava", "tap")


def _looks_like_test(name: str, cmd: str) -> bool:
    n, c = name.lower(), (cmd or "").lower()
    return ("test" in n or any(r in c for r in _RUNNERS))


def _covering_script(test_path: str, scripts: dict) -> str | None:
    """The name of the first script that would run this test, or None. A runner with
    no explicit path globs the project; otherwise a script naming the file's dir."""
    d = test_path.rsplit("/", 1)[0] if "/" in test_path else ""
    for name, cmd in scripts.items():
        c = cmd.lower()
        names_a_path = bool(re.search(r"\b(tools|src|tests?|app|lib)/", c))
        if any(r in c for r in _RUNNERS) and not names_a_path:
            return name          # runner globs the whole project
        if d and d.lower() in c:
            return name          # script targets this dir
        if test_path.lower() in c:
            return name
    return None


# ------------------------------------------------- CI workflow actually runs it
def _workflow_scripts(repo_dir: Path) -> set[str]:
    """Script names a GitHub workflow actually invokes (npm run X / pnpm X / yarn X).
    A test in a package.json script that no workflow runs is not run by CI."""
    wf = repo_dir / ".github" / "workflows"
    names: set[str] = set()
    if wf.is_dir():
        for f in list(wf.glob("*.yml")) + list(wf.glob("*.yaml")):
            t = _read(f, 30000)
            for m in re.finditer(r"(?:npm run|pnpm(?:\s+run)?|yarn(?:\s+run)?)\s+([\w:.\-]+)", t):
                names.add(m.group(1))
    return names


# ------------------------------------------------------- PII written to a log
_PII = (r"recipient|e[-_]?mail\b|email|iban|bic|phone|telefon|address|adresse|"
        r"vorname|nachname|geburts|zaehlpunkt|zählpunkt|kunden(?:name|nummer)")
_LOG_PII = re.compile(
    r"(console\.(?:log|error|warn|info|debug)|logger\.\w+|log\.\w+)\s*\([^)]*"
    r"\$\{[^}]*(?:" + _PII + r")[^}]*\}", re.IGNORECASE)


def pii_in_logs(diff: str) -> list[dict]:
    """Added log statements that interpolate a personal-data field. A log line is
    telemetry; per the repos' own rule personal data must not land there. Matches
    an interpolated PII variable (not a static label), so it is high-signal."""
    findings, cur = [], None
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:].strip()
            continue
        if ln.startswith("+") and not ln.startswith("+++") and _LOG_PII.search(ln):
            findings.append({
                "severity": "medium", "file": cur,
                "detail": ("This log statement interpolates a personal-data field "
                           f"(`{ln.strip()[1:][:90]}`). A log/telemetry line is not an "
                           "allowed home for personal data — log a hash or an id instead "
                           "of the address/email/name."),
                "angle": "security", "confidence": 70, "verdict": "plausible", "seed": True})
    return findings[:6]


# ------------------------------------------------------------- enclosing code
def enclosing(changed_files: list[str], repo_name: str | None, cap: int = 24000) -> str:
    """The full base text of the smaller changed files, so the reviewer sees the
    function a hunk sits in and its neighbours, not only the +/- lines."""
    d = clone_dir(repo_name)
    if not d:
        return ""
    out, used = [], 0
    for f in (changed_files or [])[:12]:
        if _is_test_path(f):
            continue
        t = _read(d / f, cap=6000)
        if not t:
            continue
        block = f"### {f} (base)\n{t}"
        if used + len(block) > cap:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


# -------------------------------------------------------------- git history
def _git(repo_dir: Path, args: list[str]) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo_dir), *args],
                           capture_output=True, text=True, timeout=20)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def history(repo_name: str | None, changed_files: list[str], per_file: int = 4) -> str:
    """Recent commits on each changed file — the 'why does this code look like this,
    is the diff re-introducing something a recent fix removed' angle (blame/log)."""
    d = clone_dir(repo_name)
    if not d:
        return ""
    out = []
    for f in (changed_files or [])[:8]:
        if _is_test_path(f):
            continue
        log = _git(d, ["log", f"-{per_file}", "--oneline", "--no-merges", "--", f]).strip()
        if log:
            out.append(f"{f}:\n" + "\n".join("  " + l for l in log.splitlines()))
    return "\n".join(out)


# ------------------------------------------------------------------- sanity
_GENERATED = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              ".generated.", ".min.js", "/dist/", "/build/")


def sanity(diff: str, changed_files: list[str]) -> list[str]:
    """Cheap pre-review smell checks: a change whose shape does not match a normal
    targeted edit (stray generated files, a wrong-base-looking deletion count, a
    migration that needs ordering) — surfaced so the reviewer looks before trusting."""
    notes = []
    gen = [f for f in (changed_files or []) if any(g in f for g in _GENERATED)]
    if gen:
        notes.append(f"touches generated/lock files ({', '.join(gen[:4])}) — confirm "
                     f"these are intended and not a stray commit")
    adds = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    if dels > max(60, adds * 3):
        notes.append(f"heavy deletions ({dels} removed vs {adds} added) — verify this is "
                     f"not built on the wrong base (a stale checkout looks like this)")
    migs = [f for f in (changed_files or [])
            if "migration" in f.lower() or "alembic/" in f.lower() or f.endswith(".sql")]
    if migs:
        notes.append(f"adds a DB migration ({', '.join(migs[:3])}) — check migration-vs-code "
                     f"ordering: can the old code run against the new schema during rollout?")
    return notes


# ------------------------------------------- past review comments (angle E)
def _past_review(repo_name: str | None, changed_files: list[str], cap: int = 8) -> str:
    """What human reviewers flagged on these exact files before — the whole-history
    version of the previous-comment angle. Best-effort; the index may be empty."""
    if not repo_name or not changed_files:
        return ""
    try:
        from . import pr_history
        rows = pr_history.for_files(repo_name, changed_files, limit=cap)
    except Exception:
        return ""
    out = []
    for r in rows:
        loc = r.get("path") or ""
        if r.get("line"):
            loc += f":{r['line']}"
        body = " ".join((r.get("body") or "").split())[:280]
        out.append(f"- @{r.get('author')} on {loc} (PR #{r.get('number')}): {body}")
    return "\n".join(out)


# ------------------------------------------------------------------ assemble
def build(repo_name: str | None, pr_body: str, diff: str,
          changed_files: list[str], memory_block: str = "") -> dict:
    """Everything the review should be grounded in. Seed findings are the
    deterministic ones (reachability, CI-wiring) the LLM must carry."""
    acc = acceptance_criteria(pr_body, repo_name)
    seeds = (reachability(diff, repo_name, changed_files)
             + ci_wiring(diff, repo_name, changed_files)
             + pii_in_logs(diff))
    conv = conventions(repo_name)
    encl = enclosing(changed_files, repo_name)
    hist = history(repo_name, changed_files)
    notes = sanity(diff, changed_files)
    past = _past_review(repo_name, changed_files)
    parts = []
    if past:
        parts.append("PAST REVIEW COMMENTS on these files (human reviewers flagged this "
                     "area before — verify the change does not re-introduce or ignore any "
                     "of these; a recurrence is almost always a real finding):\n" + past)
    if notes:
        parts.append("PRE-REVIEW SANITY (look before trusting the diff):\n"
                     + "\n".join("  - " + n for n in notes))
    if conv:
        parts.append("REPO CONVENTIONS (the repo's own rules — cite the specific one a "
                     "finding violates):\n" + conv)
    if memory_block:
        parts.append("REPO MEMORY (past lessons and gotchas on this repo):\n" + memory_block)
    if acc["criteria"]:
        crit = "\n".join(f"  [{i+1}] {c}" for i, c in enumerate(acc["criteria"]))
        src = f" (from {acc['source']}" + (f", plan {acc['plan_ref']}" if acc["plan_ref"] else "") + ")"
        parts.append(f"ACCEPTANCE CRITERIA{src} — for EACH, state met/unmet with diff "
                     f"evidence; ANY unmet criterion is changes-requested:\n{crit}")
    if seeds:
        parts.append("MECHANICAL FINDINGS (decided from the clone/CI config, not guessed — "
                     "confirm and keep unless you can show they are wrong):\n"
                     + json.dumps([{k: v for k, v in s.items() if k != "seed"} for s in seeds],
                                  ensure_ascii=False))
    if hist:
        parts.append("RECENT HISTORY of the changed files (does the diff re-introduce "
                     "something a recent commit fixed, or ignore why the code looks so):\n" + hist)
    if encl:
        parts.append("ENCLOSING CODE (base text around the changed files, so you can read "
                     "the whole function and its neighbours):\n" + encl)
    return {"block": "\n\n".join(parts), "seeds": seeds, "acceptance": acc,
            "has_conventions": bool(conv), "sanity": notes}
