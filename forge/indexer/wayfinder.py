"""Wayfinder — what else does this change touch, across every indexed repo?

Runs at the START of a run, before a plan is written. The single most expensive
mistake a repo-contributing engineer makes is reasoning inside one repo when the
change actually spans several: the backend route a frontend fetches, the type
two services share, the helper that exists twice.

The symbol graph cannot see those seams — separate repos share no imports and no
symbol ids. So wayfinder works on evidence, and reports it as evidence:

  route        a URL path string in one repo matches a route declared in another
  shared-name  the same name is exported, or referenced unresolved, elsewhere

Every finding carries its evidence and a confidence. Nothing here is asserted as
a fact — an engineer (or a human) decides. A wayfinder that guessed confidently
would be worse than none, because it would be trusted.
"""
from __future__ import annotations

from .store import Store

# A suffix this short matches half the internet ("/", "/id"). Require enough
# path to be meaningful before we claim two repos are talking.
MIN_ROUTE_SUFFIX = 5
MAX_EVIDENCE = 12


def _segments(path: str) -> int:
    """Non-empty path segments — the specificity of a route."""
    return len([p for p in path.split("/") if p])


def _repos(store: Store) -> dict[int, str]:
    return {r["id"]: r["name"]
            for r in store.db.execute("SELECT id, name FROM repos")}


def _repo_id(store: Store, name: str) -> int:
    row = store.db.execute("SELECT id FROM repos WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"repo not indexed: {name}")
    return row["id"]


def _route_matches(store: Store, repo_id: int, paths: list[str]) -> list[dict]:
    """Other repos' path literals that extend one of ours.

    A backend declares "/start" under a router mounted at "/api/v1/ace"; the
    frontend writes the whole thing. So we match on suffix, then look for the
    remaining prefix as a literal in our own repo — if the prefix is there too,
    the pairing is corroborated rather than coincidental.
    """
    if not paths:
        return []
    own_literals = {r["value"] for r in store.db.execute(
        "SELECT DISTINCT value FROM literals WHERE repo_id=?", (repo_id,))}

    rows = store.db.execute(
        "SELECT l.value, l.line, l.repo_id, f.path AS file, r.name AS repo"
        "  FROM literals l"
        "  JOIN files f ON f.id = l.file_id"
        "  JOIN repos r ON r.id = l.repo_id"
        " WHERE l.repo_id != ?", (repo_id,)).fetchall()

    out: list[dict] = []
    for row in rows:
        other = row["value"]
        for mine in paths:
            if len(mine) < MIN_ROUTE_SUFFIX:
                continue
            if other == mine:
                # A one-segment path like "/test" or "/stream" is shared by
                # codebases that have nothing to do with each other. Only a
                # path specific enough to be a real endpoint counts on its own.
                if _segments(mine) < 2:
                    continue
                conf, matched = "high", mine
            elif other.endswith(mine) and other[: -len(mine)]:
                prefix = other[: -len(mine)].rstrip("/")
                corroborated = prefix in own_literals
                # "/start" only means something when the caller reached it
                # through a mount point we also declare, e.g. "/api/v1/ace".
                if _segments(mine) < 2 and not (corroborated and _segments(prefix) >= 2):
                    continue
                conf = "high" if corroborated else "medium"
                matched = mine
            else:
                continue
            out.append({"repo": row["repo"], "file": row["file"],
                        "line": row["line"], "their_path": other,
                        "our_path": matched, "confidence": conf})
            break
    order = {"high": 0, "medium": 1}
    out.sort(key=lambda d: (order.get(d["confidence"], 2), d["repo"], d["file"]))
    return out


def _name_matches(store: Store, repo_id: int, names: list[str]) -> list[dict]:
    """Same name, other repo — either defined there too, or called there."""
    if not names:
        return []
    qs = ",".join("?" * len(names))
    out: list[dict] = []

    for row in store.db.execute(
        f"SELECT s.name, s.qualname, s.kind, f.path AS file, s.start_line,"
        f"       r.name AS repo"
        f"  FROM symbols s JOIN files f ON f.id = s.file_id"
        f"  JOIN repos r ON r.id = s.repo_id"
        f" WHERE s.repo_id != ? AND s.name IN ({qs})"
        f" LIMIT 60", (repo_id, *names)):
        out.append({"repo": row["repo"], "file": row["file"],
                    "line": row["start_line"], "name": row["name"],
                    "what": f"also defined here as {row['kind']} {row['qualname']}",
                    "confidence": "medium"})

    for row in store.db.execute(
        f"SELECT r2.name AS repo, f.path AS file, rf.line, rf.name"
        f"  FROM refs rf JOIN files f ON f.id = rf.file_id"
        f"  JOIN repos r2 ON r2.id = rf.repo_id"
        f" WHERE rf.repo_id != ? AND rf.dst_symbol_id IS NULL"
        f"   AND rf.name IN ({qs}) LIMIT 60", (repo_id, *names)):
        out.append({"repo": row["repo"], "file": row["file"],
                    "line": row["line"], "name": row["name"],
                    "what": "called here, resolving to nothing in that repo",
                    "confidence": "low"})
    return out


def impacts(store: Store, repo: str, symbol: str | None = None,
            path: str | None = None) -> dict:
    """Impact report for a symbol or a file, within and beyond its repo."""
    if not symbol and not path:
        raise ValueError("pass a symbol or a file path")
    rid = _repo_id(store, repo)
    db = store.db

    targets: list[dict] = []
    if symbol:
        targets = [dict(r) for r in db.execute(
            "SELECT s.id, s.name, s.qualname, s.kind, s.start_line, s.file_id,"
            "       f.path FROM symbols s JOIN files f ON f.id = s.file_id"
            " WHERE s.repo_id=? AND (s.name=? OR s.qualname=?)", (rid, symbol, symbol))]
        if not targets:
            raise ValueError(f"no symbol {symbol!r} in {repo}")
    else:
        frow = db.execute(
            "SELECT id, path FROM files WHERE repo_id=? AND path=?"
            " UNION SELECT id, path FROM files WHERE repo_id=? AND path LIKE ?"
            " LIMIT 1", (rid, path, rid, f"%{path}")).fetchone()
        if frow is None:
            raise ValueError(f"no file {path!r} in {repo}")
        targets = [dict(r) for r in db.execute(
            "SELECT s.id, s.name, s.qualname, s.kind, s.start_line, s.file_id,"
            "       f.path FROM symbols s JOIN files f ON f.id = s.file_id"
            " WHERE s.file_id=?", (frow["id"],))]

    file_ids = sorted({t["file_id"] for t in targets})
    names = sorted({t["name"] for t in targets})

    # in-repo: direct callers of the targeted symbols
    local: list[dict] = []
    if targets:
        qs = ",".join("?" * len(targets))
        local = [dict(r) for r in db.execute(
            f"SELECT DISTINCT f.path, rf.line, s.qualname AS caller"
            f"  FROM refs rf JOIN files f ON f.id = rf.file_id"
            f"  LEFT JOIN symbols s ON s.id = rf.src_symbol_id"
            f" WHERE rf.dst_symbol_id IN ({qs})"
            f" ORDER BY f.path, rf.line LIMIT 200",
            [t["id"] for t in targets])]

    # the path literals living in the targeted files are our cross-repo seam
    qs = ",".join("?" * len(file_ids)) if file_ids else "NULL"
    paths = [r["value"] for r in db.execute(
        f"SELECT DISTINCT value FROM literals WHERE file_id IN ({qs})",
        file_ids)] if file_ids else []

    routes = _route_matches(store, rid, paths)
    shared = _name_matches(store, rid, names)

    by_repo: dict[str, dict] = {}
    for r in routes[:MAX_EVIDENCE * 4]:
        e = by_repo.setdefault(r["repo"], {"repo": r["repo"], "signals": {}})
        e["signals"].setdefault("route", []).append(r)
    for s in shared[:MAX_EVIDENCE * 4]:
        e = by_repo.setdefault(s["repo"], {"repo": s["repo"], "signals": {}})
        e["signals"].setdefault("shared-name", []).append(s)

    cross = []
    for entry in by_repo.values():
        best = "low"
        for sig in entry["signals"].values():
            for item in sig:
                if item["confidence"] == "high":
                    best = "high"
                elif item["confidence"] == "medium" and best != "high":
                    best = "medium"
        entry["confidence"] = best
        entry["signals"] = {k: v[:MAX_EVIDENCE] for k, v in entry["signals"].items()}
        cross.append(entry)
    cross.sort(key=lambda e: {"high": 0, "medium": 1}.get(e["confidence"], 2))

    notes = []
    indexed = list(_repos(store).values())
    if len(indexed) < 2:
        notes.append("only one repo is indexed — cross-repo impact cannot be "
                     "assessed. Index the sibling repos first.")
    if not paths and symbol:
        notes.append("the target declares no URL paths, so route coupling was "
                     "not checked for it.")

    return {
        "target": {"repo": repo, "symbol": symbol, "path": path,
                   "matched": [{"qualname": t["qualname"], "kind": t["kind"],
                                "file": t["path"], "line": t["start_line"]}
                               for t in targets[:20]],
                   "match_count": len(targets)},
        "indexed_repos": indexed,
        "local_callers": local,
        "cross_repo": cross,
        "notes": notes,
    }


def repo_links(store: Store) -> list[dict]:
    """Which indexed repos look coupled, and how strongly. A map for the UI."""
    repos = _repos(store)
    out = []
    for rid, name in repos.items():
        paths = [r["value"] for r in store.db.execute(
            "SELECT DISTINCT value FROM literals WHERE repo_id=?", (rid,))]
        hits = _route_matches(store, rid, paths)
        agg: dict[str, dict] = {}
        for h in hits:
            a = agg.setdefault(h["repo"], {"to": h["repo"], "routes": 0, "high": 0})
            a["routes"] += 1
            if h["confidence"] == "high":
                a["high"] += 1
        for a in agg.values():
            out.append({"from": name, **a})
    out.sort(key=lambda d: -d["high"])
    return out
