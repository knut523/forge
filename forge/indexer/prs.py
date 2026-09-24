"""Open pull requests, with the one thing GitHub cannot tell you.

GitHub already lists PRs perfectly well. What it cannot show is whether a PR
reaches *other repositories* — because that lives in the call graph and the
route strings, not in the diff. This module joins the two: the PR's changed
files come from GitHub, and the blast radius of those files comes from the local
index and the wayfinder.

Read-only throughout. Listing and reading PRs uses the READ token; nothing here
can approve, merge, comment or push.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GHError(RuntimeError):
    pass


def _get(path: str, token: str | None, params: dict | None = None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "forge",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GHError("GitHub rejected the read token (401) — it is expired "
                          "or revoked. Set a fresh one in Settings.")
        if e.code == 403:
            raise GHError("GitHub refused (403) — rate limited, or the token "
                          "cannot see this repository.")
        if e.code == 404:
            raise GHError(f"not found: {path}")
        raise GHError(f"GitHub {e.code} for {path}")
    except Exception as e:
        raise GHError(f"could not reach GitHub: {type(e).__name__}")


def _slim(pr: dict, full_name: str) -> dict:
    return {
        "repo": full_name,
        "number": pr["number"],
        "title": pr["title"],
        "author": (pr.get("user") or {}).get("login"),
        "draft": bool(pr.get("draft")),
        "base": (pr.get("base") or {}).get("ref"),
        "head": (pr.get("head") or {}).get("ref"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "url": pr.get("html_url"),
        "labels": [l.get("name") for l in pr.get("labels", [])],
        "comments": pr.get("comments"),
    }


def list_open(full_names: list[str], token: str | None) -> tuple[list[dict], list[dict]]:
    """(pull requests, per-repo errors). One bad repo must not hide the rest."""
    out: list[dict] = []
    errs: list[dict] = []
    for fn in full_names:
        try:
            prs = _get(f"/repos/{fn}/pulls", token,
                       {"state": "open", "per_page": 50, "sort": "updated",
                        "direction": "desc"})
        except GHError as e:
            errs.append({"repo": fn, "error": str(e)})
            continue
        for pr in prs if isinstance(prs, list) else []:
            out.append(_slim(pr, fn))
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out, errs


def diff(owner: str, repo: str, number: int, token: str | None) -> str:
    """The PR's unified diff as raw text, via the READ token. GitHub returns the
    patch directly when asked with the diff media type."""
    url = f"{API}/repos/{owner}/{repo}/pulls/{number}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "forge",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GHError("GitHub rejected the read token (401) — expired or revoked.")
        if e.code == 403:
            raise GHError("GitHub refused (403) — rate limited, or the token cannot "
                          "see this repository.")
        raise GHError(f"GitHub {e.code} fetching the diff")
    except Exception as e:
        raise GHError(f"could not reach GitHub: {type(e).__name__}")


def comments(owner: str, repo: str, number: int, token: str | None,
             cap: int = 30) -> list[dict]:
    """Existing conversation comments on the PR, oldest first. Best-effort: a
    failure here must not sink a review, so it returns [] rather than raising."""
    try:
        items = _get(f"/repos/{owner}/{repo}/issues/{number}/comments", token,
                     {"per_page": 100})
    except GHError:
        return []
    out = [{"author": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"),
            "body": (c.get("body") or "")[:1500]}
           for c in (items if isinstance(items, list) else [])]
    return out[-cap:]


def detail(owner: str, repo: str, number: int, token: str | None) -> dict:
    fn = f"{owner}/{repo}"
    pr = _get(f"/repos/{fn}/pulls/{number}", token)
    files = _get(f"/repos/{fn}/pulls/{number}/files", token, {"per_page": 100})
    d = _slim(pr, fn)
    d.update({
        "body": (pr.get("body") or "")[:8000],
        "mergeable_state": pr.get("mergeable_state"),
        "additions": pr.get("additions"), "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "files": [{"path": f["filename"], "status": f["status"],
                   "additions": f["additions"], "deletions": f["deletions"]}
                  for f in (files if isinstance(files, list) else [])],
    })
    return d


def impact_for(pr: dict, index_store, repo_name: str | None) -> dict:
    """Cross-repo blast radius of a PR's changed files — the forge-only part.

    Best effort by design: files the index has never seen are reported as such
    rather than silently dropped, because "we found nothing" and "we could not
    look" are different answers and only one of them is reassuring.
    """
    from ..indexer import wayfinder as W

    if not repo_name:
        return {"available": False,
                "why": "this GitHub repo is not indexed locally, so its call "
                       "graph is unknown — index it to get impact"}
    hit: dict[str, dict] = {}
    checked, unknown = [], []
    for f in pr.get("files", [])[:40]:
        path = f["path"]
        try:
            rep = W.impacts(index_store, repo_name, path=path)
        except ValueError:
            unknown.append(path)
            continue
        checked.append(path)
        for c in rep.get("cross_repo", []):
            cur = hit.setdefault(c["repo"], {"repo": c["repo"], "confidence": c["confidence"],
                                             "files": [], "evidence": []})
            cur["files"].append(path)
            if c["confidence"] == "high":
                cur["confidence"] = "high"
            for sig, items in c["signals"].items():
                for i in items[:3]:
                    cur["evidence"].append({"signal": sig, **i})
    for v in hit.values():
        v["evidence"] = v["evidence"][:8]
        v["files"] = sorted(set(v["files"]))[:12]
    return {"available": True, "checked": len(checked),
            "not_indexed": unknown[:12],
            "reaches": sorted(hit.values(),
                              key=lambda x: 0 if x["confidence"] == "high" else 1)}
