"""The forge write path for review comments — the second, deliberate exception to
"forge is read-only".

forge's first write path is engine.ship_it: it opens a PR, and only after a human
approves that specific run. This module is the second: it posts a review comment to
a PR, and only when a human clicks Post on a draft they have seen and edited. Both
are explicit human actions; neither can be reached by falling through a pipeline.

The token is passed in, never read here — the single read of GITHUB_WRITE for this
path lives in the api handler that already has the human's approval in hand.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "https://api.github.com"


class WriteError(RuntimeError):
    pass


def post_comment(owner: str, repo: str, number: int, body: str,
                 write_token: str) -> dict:
    """Post one issue/PR conversation comment. Returns {id, url}."""
    if not body.strip():
        raise WriteError("refusing to post an empty comment")
    url = f"{API}/repos/{owner}/{repo}/issues/{number}/comments"
    req = urllib.request.Request(
        url, data=json.dumps({"body": body}).encode(), method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "forge",
            "Authorization": f"Bearer {write_token}",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
            return {"id": d.get("id"), "url": d.get("html_url")}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            raise WriteError("GitHub rejected the write token (401) — expired or revoked.")
        if e.code == 403:
            raise WriteError("GitHub refused (403) — the write token cannot comment on "
                             f"this repo, or is rate limited. {detail}".strip())
        if e.code == 404:
            raise WriteError(f"not found: {owner}/{repo}#{number} (or the token cannot "
                             "see it)")
        raise WriteError(f"GitHub {e.code}: {detail or 'comment failed'}")
    except urllib.error.URLError as e:
        raise WriteError(f"could not reach GitHub: {e.reason}")
