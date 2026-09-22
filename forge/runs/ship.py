"""Review, Approve, PR — the last three steps, and the only ones that leave the box.

The whole system is arranged around one rule: **nothing reaches GitHub until a
human approves this specific run.** That is enforced structurally rather than
remembered —

* the write token is read here and nowhere else, and only after the approval is
  recorded in the database;
* the PR phase is not part of the run pipeline at all. It cannot be reached by
  falling through the earlier phases; it only runs from an explicit approve call.

Everything is pushed through the GitHub API rather than a local `git push`, so
no credential is ever written to a checkout on disk.
"""
from __future__ import annotations

import base64
import difflib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".adoc")


def _api(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "forge"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"GitHub {e.code} on {method} {path}: {detail}")
    except Exception as e:
        raise RuntimeError(f"GitHub call failed: {type(e).__name__}")


# ─── review ─────────────────────────────────────────────────────────────────

def diff_for(built: dict, source_root: Path | None) -> list[dict]:
    """Unified diff of every written file against what is on disk today."""
    out = []
    ws = Path(built.get("workspace", ""))
    for f in built.get("files", []):
        rel = f["path"]
        new_p = ws / rel
        if not new_p.is_file():
            continue
        new = new_p.read_text("utf-8", "replace").splitlines(keepends=True)
        old_p = (source_root / rel) if source_root else None
        old = (old_p.read_text("utf-8", "replace").splitlines(keepends=True)
               if old_p and old_p.is_file() else [])
        d = list(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", n=3))
        adds = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
        out.append({"path": rel, "status": "modified" if old else "added",
                    "additions": adds, "deletions": dels,
                    "diff": "".join(d)[:40000]})
    return out


def review(rs, rid: str, built: dict, verified: dict, source_root: Path | None,
           impact: dict) -> dict:
    """Assemble everything a human needs to decide, then stop and wait."""
    rs.set_phase(rid, "review")
    diffs = diff_for(built, source_root)
    if not diffs:
        rs.emit(rid, "review", "warn", "Nothing to review — no files were written",
                level="warn")
        return {"ok": False}

    total_a = sum(d["additions"] for d in diffs)
    total_d = sum(d["deletions"] for d in diffs)
    docs_only = all(d["path"].lower().endswith(DOC_SUFFIXES) for d in diffs)

    rs.emit(rid, "review", "metric", "Change summary", {
        "files": len(diffs), "additions": total_a, "deletions": total_d,
        "risk": "low — documentation only" if docs_only else "code change",
        "verify": "clean" if verified.get("ok") else
                  f"{len(verified.get('problems', []))} problem(s)"})
    for d in diffs:
        rs.emit(rid, "review", "diff", f"{d['path']}  +{d['additions']} −{d['deletions']}",
                {"text": d["diff"]})

    reaches = [c["repo"] for c in (impact.get("cross_repo") or [])]
    if reaches:
        rs.emit(rid, "review", "impact",
                f"Reminder: this change reaches {', '.join(reaches)}",
                {"note": "those repos are not touched by this PR — check whether "
                         "they need a matching change"}, level="warn")

    # A UI change deserves a screenshot. Saying so beats pretending otherwise.
    if any(d["path"].endswith((".tsx", ".jsx", ".vue", ".svelte", ".css"))
           for d in diffs):
        rs.emit(rid, "review", "warn", "This touches UI files but no screenshots were taken",
                {"note": "visual verification is not implemented yet — review the "
                         "diff and check the rendered result yourself"}, level="warn")

    rs.emit(rid, "review", "done", "Ready for your decision")
    return {"ok": True, "diffs": diffs, "docs_only": docs_only,
            "additions": total_a, "deletions": total_d}


# ─── pr ─────────────────────────────────────────────────────────────────────

def _pick_base(owner_repo: str, token: str) -> tuple[str, str]:
    """Base branch, and why. House rule is `dev`; fall back honestly."""
    repo = _api("GET", f"/repos/{owner_repo}", token)
    default = repo.get("default_branch") or "main"
    for cand in ("dev", "develop"):
        try:
            _api("GET", f"/repos/{owner_repo}/branches/{cand}", token)
            return cand, f"house rule: PRs target {cand}"
        except RuntimeError:
            continue
    return default, (f"no dev/develop branch exists, so falling back to the "
                     f"default branch {default}")


def open_pr(rs, rid: str, run: dict, built: dict, reviewed: dict,
            owner_repo: str, token: str) -> dict:
    """Branch, commit each file, open the PR. Only ever called after approval."""
    rs.set_phase(rid, "pr")
    base, why = _pick_base(owner_repo, token)
    rs.emit(rid, "pr", "step", f"Base branch: {base}", {"reason": why})

    head_sha = _api("GET", f"/repos/{owner_repo}/git/ref/heads/{base}",
                    token)["object"]["sha"]
    branch = f"forge/{rid}"
    try:
        _api("POST", f"/repos/{owner_repo}/git/refs", token,
             {"ref": f"refs/heads/{branch}", "sha": head_sha})
    except RuntimeError as e:
        if "422" not in str(e):
            raise
    rs.emit(rid, "pr", "info", f"Branch {branch} created from {base}")

    ws = Path(built["workspace"])
    for d in reviewed["diffs"]:
        rel = d["path"]
        content = (ws / rel).read_text("utf-8", "replace")
        payload = {"message": f"forge: {rel}", "branch": branch,
                   "content": base64.b64encode(content.encode()).decode()}
        try:   # an update needs the blob sha of what is already there
            cur = _api("GET", f"/repos/{owner_repo}/contents/"
                              f"{urllib.parse.quote(rel)}?ref={branch}", token)
            if isinstance(cur, dict) and cur.get("sha"):
                payload["sha"] = cur["sha"]
        except RuntimeError:
            pass
        _api("PUT", f"/repos/{owner_repo}/contents/{urllib.parse.quote(rel)}",
             token, payload)
        rs.emit(rid, "pr", "file", f"Committed {rel}")

    body = _pr_body(run, reviewed)
    pr = _api("POST", f"/repos/{owner_repo}/pulls", token,
              {"title": run["goal"][:250], "head": branch, "base": base,
               "body": body, "draft": False})
    rs.emit(rid, "pr", "done", f"Opened PR #{pr['number']}",
            {"url": pr["html_url"], "base": base, "head": branch})
    return {"number": pr["number"], "url": pr["html_url"], "base": base}


def _pr_body(run: dict, reviewed: dict) -> str:
    """The description must describe the whole change, not just the last edit."""
    files = "\n".join(f"- `{d['path']}` +{d['additions']} −{d['deletions']}"
                      for d in reviewed["diffs"])
    return (
        f"## What this changes\n\n{run['goal']}\n\n"
        f"## Files\n\n{files}\n\n"
        f"## How it was produced\n\n"
        f"Built by forge run `{run['id']}` against an index of the repository, "
        f"using `{run.get('model') or 'an engineer model'}`. "
        f"{'Documentation only.' if reviewed.get('docs_only') else ''}\n\n"
        f"A human reviewed the diff and approved this run before it was opened. "
        f"Automated checks covered syntax and stub detection only — the "
        f"repository's own tests and typechecker were **not** run.\n"
    )
