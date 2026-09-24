"""PR history — the review memory of a whole org, mined once and kept local.

GitHub is the source of truth but a slow, paginated one; the PR view re-fetching it
live is why it reloads. This ingests every PR and every review comment into a local
SQLite once, so:

  * the review can ask "what did a human flag on THIS file before" (the previous-
    comment angle, at the scale of the whole history) and check for recurrence;
  * the PR view serves instantly from the index instead of hitting GitHub each time;
  * there is a clear, queryable record of *why* things changed — the reasons live in
    the comments, not the diffs.

Read-only against GitHub (list + read). It never posts, never pushes.
"""
from __future__ import annotations

import os
import sqlite3
import time

from ..indexer import github as GH

DB = os.environ.get("FORGE_PRHIST_DB", "/config/forge-prhist.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    repo TEXT, number INTEGER, title TEXT, state TEXT, author TEXT,
    merged INTEGER, base TEXT, head TEXT, url TEXT, created TEXT, updated TEXT,
    body TEXT, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT, number INTEGER, kind TEXT, author TEXT, path TEXT, line INTEGER,
    created TEXT, state TEXT, body TEXT, UNIQUE(repo, number, kind, author, created, path));
CREATE INDEX IF NOT EXISTS ix_c_repo_path ON comments(repo, path);
CREATE INDEX IF NOT EXISTS ix_c_author ON comments(author);
CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(body, content='comments', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS c_ai AFTER INSERT ON comments BEGIN
  INSERT INTO comments_fts(rowid, body) VALUES (new.id, new.body); END;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _db(path: str = DB) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _api_all(url: str, token: str, cap_pages: int = 20) -> list:
    """Paginated GET — GitHub caps per_page at 100; walk until short or cap."""
    out, page = [], 1
    while page <= cap_pages:
        sep = "&" if "?" in url else "?"
        batch = GH._api(f"{url}{sep}per_page=100&page={page}", token)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def ingest_repo(repo: str, token: str, path: str = DB, on: object = None) -> dict:
    """Fetch every PR + its reviews and comments for one owner/repo into the index."""
    ev = on or (lambda *a, **k: None)
    api = "https://api.github.com"
    c = _db(path)
    prs = _api_all(f"{api}/repos/{repo}/pulls?state=all&sort=updated&direction=desc", token)
    ev("info", f"{repo}: {len(prs)} PRs")
    n_c = 0
    for p in prs:
        num = p["number"]
        c.execute("INSERT OR REPLACE INTO prs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            repo, num, p.get("title"), p.get("state"), (p.get("user") or {}).get("login"),
            1 if p.get("merged_at") else 0, (p.get("base") or {}).get("ref"),
            (p.get("head") or {}).get("ref"), p.get("html_url"),
            p.get("created_at"), p.get("updated_at"), (p.get("body") or "")[:8000]))
        for kind, url in (("review", f"{api}/repos/{repo}/pulls/{num}/reviews"),
                          ("inline", f"{api}/repos/{repo}/pulls/{num}/comments"),
                          ("issue", f"{api}/repos/{repo}/issues/{num}/comments")):
            try:
                items = _api_all(url, token, cap_pages=5)
            except GH.GitHubError:
                continue
            for it in items:
                body = (it.get("body") or "").strip()
                if not body:
                    continue
                try:
                    c.execute(
                        "INSERT OR IGNORE INTO comments"
                        " (repo,number,kind,author,path,line,created,state,body)"
                        " VALUES (?,?,?,?,?,?,?,?,?)", (
                            repo, num, kind, (it.get("user") or {}).get("login"),
                            it.get("path"), it.get("line") or it.get("original_line"),
                            it.get("created_at") or it.get("submitted_at"),
                            it.get("state"), body[:6000]))
                    n_c += c.total_changes and 1 or 0
                except sqlite3.Error:
                    pass
        c.commit()
    c.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest', ?)",
              (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
    c.commit()
    stats = {"repo": repo, "prs": len(prs),
             "comments": c.execute("SELECT COUNT(*) FROM comments WHERE repo=?", (repo,)).fetchone()[0]}
    c.close()
    return stats


def for_files(repo: str, files: list[str], path: str = DB, limit: int = 20) -> list[dict]:
    """Past review comments on any of these files — the 'a reviewer flagged this area
    before' signal for a new review. Newest first."""
    if not files:
        return []
    c = _db(path)
    q = ("SELECT number,kind,author,path,line,created,body FROM comments"
         " WHERE repo=? AND path IN (%s) AND author != '' ORDER BY created DESC LIMIT ?"
         % ",".join("?" * len(files)))
    rows = [dict(r) for r in c.execute(q, (repo, *files, limit)).fetchall()]
    c.close()
    return rows


def search(query: str, repo: str | None = None, path: str = DB, limit: int = 20) -> list[dict]:
    """Full-text over every review comment — the queryable 'why' across all PRs."""
    c = _db(path)
    term = '"' + query.replace('"', '""') + '"'
    sql = ("SELECT cm.repo,cm.number,cm.author,cm.path,cm.body FROM comments_fts f"
           " JOIN comments cm ON cm.id=f.rowid WHERE comments_fts MATCH ?"
           + (" AND cm.repo=?" if repo else "") + " ORDER BY rank LIMIT ?")
    args = (term, repo, limit) if repo else (term, limit)
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


def stats(path: str = DB) -> dict:
    c = _db(path)
    out = {"prs": c.execute("SELECT COUNT(*) FROM prs").fetchone()[0],
           "comments": c.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
           "by_author": [dict(r) for r in c.execute(
               "SELECT author, COUNT(*) n FROM comments GROUP BY author ORDER BY n DESC LIMIT 8")],
           "last_ingest": (c.execute("SELECT v FROM meta WHERE k='last_ingest'").fetchone() or [None])[0]}
    c.close()
    return out
