"""GitHub read path — list an org's repos, clone them shallow, index them.

This module is **read-only by contract**. It lists and clones; it never pushes,
never opens a PR, never writes to GitHub. That is not an accident of what is
implemented so far — it is the boundary. The write token lives elsewhere and is
unlocked only after a human approves a specific run.

The token is never persisted: it is used for the clone and then scrubbed from
the remote URL, so an indexed checkout left on disk carries no credential.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
READ_TOKEN_ENV = "FORGE_READ_TOKEN"


class GitHubError(RuntimeError):
    pass


def read_token() -> str | None:
    tok = os.environ.get(READ_TOKEN_ENV, "").strip()
    return tok or None


def _api(url: str, token: str | None) -> list | dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "forge-indexer",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            import json
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GitHubError(
                f"GitHub refused the request ({e.code}). Set a read token in "
                f"${READ_TOKEN_ENV} — an unauthenticated call is limited to "
                f"60/hour and cannot see private repos.")
        if e.code == 404:
            raise GitHubError(f"not found: {url}")
        raise GitHubError(f"GitHub {e.code} for {url}")


def list_org_repos(org: str, token: str | None = None, *,
                   include_archived: bool = False,
                   include_forks: bool = False,
                   limit: int = 300) -> list[dict]:
    """Every repo in an org (or, failing that, a user account)."""
    out: list[dict] = []
    for scope in ("orgs", "users"):
        out = []
        page = 1
        try:
            while len(out) < limit:
                batch = _api(
                    f"{API}/{scope}/{org}/repos?per_page=100&page={page}&sort=pushed",
                    token)
                if not isinstance(batch, list) or not batch:
                    break
                out.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        except GitHubError:
            if scope == "users":
                raise
            continue                      # not an org — try the user endpoint
        break

    repos = []
    for r in out[:limit]:
        if r.get("archived") and not include_archived:
            continue
        if r.get("fork") and not include_forks:
            continue
        repos.append({
            "name": r["name"],
            "full_name": r["full_name"],
            "private": bool(r.get("private")),
            "default_branch": r.get("default_branch") or "main",
            "clone_url": r["clone_url"],
            "pushed_at": r.get("pushed_at"),
            "language": r.get("language"),
            "size_kb": r.get("size", 0),
        })
    return repos


def clone(repo: dict, dest_root: str | Path, token: str | None = None,
          depth: int = 1) -> Path:
    """Shallow-clone one repo and scrub the credential from the checkout."""
    dest = Path(dest_root) / repo["name"]
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = repo["clone_url"]
    auth_url = (url.replace("https://", f"https://x-access-token:{token}@")
                if token else url)
    cmd = ["git", "clone", "--depth", str(depth), "--single-branch",
           "--branch", repo["default_branch"], auth_url, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        err = (proc.stderr or "").replace(token or "\0", "***")[:300]
        raise GitHubError(f"clone failed for {repo['full_name']}: {err}")

    # The token would otherwise sit in .git/config forever.
    subprocess.run(["git", "-C", str(dest), "remote", "set-url", "origin", url],
                   capture_output=True, timeout=30)
    return dest
