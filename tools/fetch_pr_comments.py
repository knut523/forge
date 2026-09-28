"""Read-only: fetch all non-author comments on knut523's open WirStrom1 PRs.
Uses forge's own github read client (token stays in-process). Comments are not secrets."""
import json
import forge.indexer.github as gh

ORG = "WirStrom1"
AUTHOR = "knut523"
tok = gh.read_token(ORG)
if not tok:
    print("NO READ TOKEN"); raise SystemExit(1)

res = gh._api(
    f"https://api.github.com/search/issues?q=is:pr+is:open+author:{AUTHOR}+org:{ORG}&per_page=100", tok)
prs = res.get("items", []) if isinstance(res, dict) else []
print(f"open {AUTHOR} PRs in {ORG}: {len(prs)}\n")

out = []
for pr in prs:
    repo = pr["repository_url"].split("/repos/")[1]
    n = pr["number"]
    entry = {"repo": repo, "number": n, "title": pr["title"],
             "url": pr["html_url"], "updated": pr["updated_at"], "comments": []}
    # issue-level + inline review comments
    for kind, url in [
        ("issue", f"https://api.github.com/repos/{repo}/issues/{n}/comments?per_page=100"),
        ("inline", f"https://api.github.com/repos/{repo}/pulls/{n}/comments?per_page=100")]:
        try:
            for c in gh._api(url, tok):
                a = (c.get("user") or {}).get("login")
                if a and a != AUTHOR:
                    entry["comments"].append({
                        "kind": kind, "author": a,
                        "path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                        "created": c.get("created_at"),
                        "body": (c.get("body") or "").strip()})
        except Exception as e:
            entry["comments"].append({"error": f"{kind}: {e}"})
    # review summaries (approve / request-changes bodies)
    try:
        for r in gh._api(f"https://api.github.com/repos/{repo}/pulls/{n}/reviews?per_page=100", tok):
            a = (r.get("user") or {}).get("login")
            body = (r.get("body") or "").strip()
            if a and a != AUTHOR and (body or r.get("state") in ("CHANGES_REQUESTED", "APPROVED")):
                entry["comments"].append({
                    "kind": "review", "author": a, "state": r.get("state"),
                    "created": r.get("submitted_at"), "body": body})
    except Exception as e:
        entry["comments"].append({"error": f"reviews: {e}"})
    out.append(entry)

# print compactly, only PRs that actually have foreign comments
authors = {}
for e in out:
    for c in e["comments"]:
        if c.get("author"):
            authors[c["author"]] = authors.get(c["author"], 0) + 1
print("comment authors (non-knut523):", json.dumps(authors), "\n")
print(json.dumps([e for e in out if e["comments"]], indent=2, ensure_ascii=False))
