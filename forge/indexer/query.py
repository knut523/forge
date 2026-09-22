"""Read API over the index — the questions an engineer actually asks.

Every function here answers one grounding question that would otherwise cost a
model several greps and a lot of guessing: where is this defined, who calls it,
what breaks if I change it, what does this file hang off.
"""
from __future__ import annotations

from .store import Store


def _repo_id(store: Store, repo: str | None) -> int:
    if repo:
        row = store.db.execute("SELECT id FROM repos WHERE name=?", (repo,)).fetchone()
        if row is None:
            raise ValueError(f"repo not indexed: {repo}")
        return row["id"]
    rows = store.db.execute("SELECT id, name FROM repos ORDER BY id").fetchall()
    if not rows:
        raise ValueError("no repos indexed yet")
    if len(rows) > 1:
        names = ", ".join(r["name"] for r in rows)
        raise ValueError(f"several repos indexed ({names}) — pass --repo")
    return rows[0]["id"]


def list_repos(store: Store) -> list[dict]:
    return [dict(r) for r in store.db.execute(
        "SELECT name, branch, head_sha, origin, indexed_at, file_count,"
        " symbol_count, ref_count, resolved_pct FROM repos ORDER BY name")]


def overview(store: Store, repo: str | None = None) -> dict:
    rid = _repo_id(store, repo)
    db = store.db
    head = dict(db.execute(
        "SELECT name, root, branch, head_sha, indexed_at, file_count, symbol_count,"
        " ref_count, resolved_pct FROM repos WHERE id=?", (rid,)).fetchone())
    head["languages"] = [dict(r) for r in db.execute(
        "SELECT lang, COUNT(*) AS files, SUM(loc) AS loc FROM files"
        " WHERE repo_id=? GROUP BY lang ORDER BY loc DESC", (rid,))]
    head["kinds"] = [dict(r) for r in db.execute(
        "SELECT kind, COUNT(*) AS n FROM symbols WHERE repo_id=?"
        " GROUP BY kind ORDER BY n DESC", (rid,))]
    head["largest_files"] = [dict(r) for r in db.execute(
        "SELECT path, loc FROM files WHERE repo_id=? ORDER BY loc DESC LIMIT 10", (rid,))]
    head["parse_errors"] = [dict(r) for r in db.execute(
        "SELECT path, parse_error FROM files WHERE repo_id=? AND parse_error IS NOT NULL"
        " LIMIT 20", (rid,))]
    return head


def search(store: Store, q: str, repo: str | None = None, limit: int = 25) -> list[dict]:
    """Full-text over symbol names, signatures and docs."""
    rid = _repo_id(store, repo)
    # quote the term so FTS treats user input as a literal, not syntax
    term = '"' + q.replace('"', '""') + '"*'
    rows = store.db.execute(
        "SELECT s.id, s.name, s.qualname, s.kind, s.signature, s.doc,"
        "       f.path, s.start_line"
        "  FROM symbols_fts ft"
        "  JOIN symbols s ON s.id = ft.rowid"
        "  JOIN files   f ON f.id = s.file_id"
        " WHERE symbols_fts MATCH ? AND s.repo_id = ?"
        " ORDER BY rank LIMIT ?", (term, rid, limit)).fetchall()
    return [dict(r) for r in rows]


def find_symbol(store: Store, name: str, repo: str | None = None) -> list[dict]:
    """Exact definitions for a name or qualname — the 'where is Foo' answer."""
    rid = _repo_id(store, repo)
    rows = store.db.execute(
        "SELECT s.id, s.name, s.qualname, s.kind, s.signature, s.doc, s.exported,"
        "       s.start_line, s.end_line, f.path"
        "  FROM symbols s JOIN files f ON f.id = s.file_id"
        " WHERE s.repo_id=? AND (s.name=? OR s.qualname=?)"
        " ORDER BY s.exported DESC, f.path", (rid, name, name)).fetchall()
    return [dict(r) for r in rows]


def callers(store: Store, symbol_id: int, limit: int = 100) -> list[dict]:
    rows = store.db.execute(
        "SELECT r.line, r.full, r.kind, f.path,"
        "       s.qualname AS caller, s.kind AS caller_kind, s.id AS caller_id"
        "  FROM refs r"
        "  JOIN files f ON f.id = r.file_id"
        "  LEFT JOIN symbols s ON s.id = r.src_symbol_id"
        " WHERE r.dst_symbol_id = ? ORDER BY f.path, r.line LIMIT ?",
        (symbol_id, limit)).fetchall()
    return [dict(r) for r in rows]


def callees(store: Store, symbol_id: int, limit: int = 100) -> list[dict]:
    rows = store.db.execute(
        "SELECT DISTINCT d.id, d.qualname, d.kind, f.path, d.start_line"
        "  FROM refs r"
        "  JOIN symbols d ON d.id = r.dst_symbol_id"
        "  JOIN files f ON f.id = d.file_id"
        " WHERE r.src_symbol_id = ? ORDER BY d.qualname LIMIT ?",
        (symbol_id, limit)).fetchall()
    return [dict(r) for r in rows]


def blast_radius(store: Store, symbol_id: int, depth: int = 2,
                 cap: int = 400) -> dict:
    """Transitive callers — what a change to this symbol can reach.

    Breadth-first with a hard cap: on a hub symbol the true closure is most of
    the repo, and an engineer is better served by 'this is a hub, here are the
    first N' than by a dump of everything.
    """
    seen: set[int] = {symbol_id}
    layers: list[list[dict]] = []
    frontier = [symbol_id]
    truncated = False
    for _ in range(max(1, depth)):
        if not frontier:
            break
        qs = ",".join("?" * len(frontier))
        rows = store.db.execute(
            f"SELECT DISTINCT s.id, s.qualname, s.kind, f.path, s.start_line"
            f"  FROM refs r JOIN symbols s ON s.id = r.src_symbol_id"
            f"  JOIN files f ON f.id = s.file_id"
            f" WHERE r.dst_symbol_id IN ({qs})", frontier).fetchall()
        layer = [dict(r) for r in rows if r["id"] not in seen]
        for r in layer:
            seen.add(r["id"])
        if len(seen) > cap:
            layer = layer[: max(0, cap - (len(seen) - len(layer)))]
            truncated = True
        layers.append(layer)
        frontier = [r["id"] for r in layer]
        if truncated:
            break
    return {"symbol_id": symbol_id, "layers": layers,
            "total": sum(len(l) for l in layers), "truncated": truncated}


def file_card(store: Store, path: str, repo: str | None = None) -> dict:
    """Everything known about one file — the unit an engineer edits."""
    rid = _repo_id(store, repo)
    f = store.db.execute(
        "SELECT id, path, lang, loc, bytes, sha, parse_error FROM files"
        " WHERE repo_id=? AND path=?", (rid, path)).fetchone()
    if f is None:
        hit = store.db.execute(
            "SELECT id, path, lang, loc, bytes, sha, parse_error FROM files"
            " WHERE repo_id=? AND path LIKE ? ORDER BY LENGTH(path) LIMIT 1",
            (rid, f"%{path}")).fetchone()
        if hit is None:
            raise ValueError(f"file not indexed: {path}")
        f = hit
    out = dict(f)
    out["symbols"] = [dict(r) for r in store.db.execute(
        "SELECT id, name, qualname, kind, start_line, end_line, signature, exported"
        "  FROM symbols WHERE file_id=? ORDER BY start_line", (f["id"],))]
    out["imports"] = [dict(r) for r in store.db.execute(
        "SELECT i.module, i.symbol, i.alias, i.line, t.path AS resolved"
        "  FROM imports i LEFT JOIN files t ON t.id = i.resolved_file_id"
        " WHERE i.file_id=? ORDER BY i.line", (f["id"],))]
    out["imported_by"] = [dict(r) for r in store.db.execute(
        "SELECT DISTINCT f2.path FROM imports i JOIN files f2 ON f2.id = i.file_id"
        " WHERE i.resolved_file_id=? ORDER BY f2.path LIMIT 100", (f["id"],))]
    return out


def hotspots(store: Store, repo: str | None = None, limit: int = 20) -> list[dict]:
    """Most-referenced symbols — the load-bearing walls of the codebase."""
    rid = _repo_id(store, repo)
    rows = store.db.execute(
        "SELECT s.id, s.qualname, s.kind, f.path, COUNT(r.id) AS refs"
        "  FROM symbols s JOIN files f ON f.id = s.file_id"
        "  JOIN refs r ON r.dst_symbol_id = s.id"
        " WHERE s.repo_id=? GROUP BY s.id ORDER BY refs DESC LIMIT ?",
        (rid, limit)).fetchall()
    return [dict(r) for r in rows]


def orphans(store: Store, repo: str | None = None, limit: int = 50) -> list[dict]:
    """Exported symbols nothing references — dead code candidates.

    'Candidates' is load-bearing: an entry point, a route handler or a test
    fixture is legitimately unreferenced inside the repo.
    """
    rid = _repo_id(store, repo)
    rows = store.db.execute(
        "SELECT s.id, s.qualname, s.kind, f.path, s.start_line"
        "  FROM symbols s JOIN files f ON f.id = s.file_id"
        " WHERE s.repo_id=? AND s.exported=1 AND s.kind IN ('function','class')"
        "   AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.dst_symbol_id = s.id)"
        " ORDER BY f.path, s.start_line LIMIT ?", (rid, limit)).fetchall()
    return [dict(r) for r in rows]
