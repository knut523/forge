"""forge CLI — index a repo, then interrogate the graph.

    python -m forge.cli index /path/to/repo --name olaf
    python -m forge.cli overview --repo olaf
    python -m forge.cli search "tariff price"
    python -m forge.cli sym calculateTariff
    python -m forge.cli callers calculateTariff
    python -m forge.cli blast calculateTariff --depth 3
    python -m forge.cli file src/lib/tariff.ts
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .indexer.index import index_repo
from .indexer import query as Q
from .indexer.store import Store

DEFAULT_DB = os.environ.get("FORGE_DB", "/data/forge.db")


def _out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
        return
    print(_render(obj))


def _render(obj, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, list):
        if not obj:
            return pad + "(none)"
        return "\n".join(_render(o, indent) for o in obj)
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            if isinstance(v, (list, dict)):
                parts.append(f"{pad}{k}:")
                parts.append(_render(v, indent + 1))
            else:
                parts.append(f"{pad}{k}: {v}")
        return "\n".join(parts)
    return pad + str(obj)


def _one_symbol(store: Store, name: str, repo: str | None) -> dict:
    """Resolve a name to a single symbol, or explain the ambiguity."""
    hits = Q.find_symbol(store, name, repo)
    if not hits:
        print(f"no symbol named {name!r}", file=sys.stderr)
        raise SystemExit(2)
    if len(hits) > 1:
        print(f"{len(hits)} definitions match {name!r}; using the first:",
              file=sys.stderr)
        for h in hits[:8]:
            print(f"  {h['path']}:{h['start_line']}  {h['kind']} {h['qualname']}",
                  file=sys.stderr)
    return hits[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="forge", description="code index + graph")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"index db (default {DEFAULT_DB})")
    ap.add_argument("--repo", default=None, help="repo name when several are indexed")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="index a working tree")
    p.add_argument("path")
    p.add_argument("--name", default=None)
    p.add_argument("--git-only", action="store_true",
                   help="index only git-tracked/untracked files, honouring .gitignore "
                        "(default: crawl the filesystem, which also catches gitignored source)")

    sub.add_parser("repos", help="list indexed repos")
    sub.add_parser("overview", help="repo summary: languages, symbol kinds, big files")
    sub.add_parser("hotspots", help="most-referenced symbols")
    sub.add_parser("orphans", help="exported symbols nothing references")

    p = sub.add_parser("search", help="full-text symbol search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)

    for cmd, helptext in (("sym", "definitions of a name"),
                          ("callers", "what calls this symbol"),
                          ("callees", "what this symbol calls")):
        p = sub.add_parser(cmd, help=helptext)
        p.add_argument("name")

    p = sub.add_parser("blast", help="transitive callers of a symbol")
    p.add_argument("name")
    p.add_argument("--depth", type=int, default=2)

    p = sub.add_parser("file", help="everything known about one file")
    p.add_argument("path")

    a = ap.parse_args(argv)

    if a.cmd == "index":
        stats = index_repo(a.path, a.name, a.db,
                           progress=lambda m: print(m, flush=True),
                           git_only=a.git_only)
        _out(stats, a.json)
        return 0

    store = Store(a.db)
    try:
        if a.cmd == "repos":
            _out(Q.list_repos(store), a.json)
        elif a.cmd == "overview":
            _out(Q.overview(store, a.repo), a.json)
        elif a.cmd == "hotspots":
            _out(Q.hotspots(store, a.repo), a.json)
        elif a.cmd == "orphans":
            _out(Q.orphans(store, a.repo), a.json)
        elif a.cmd == "search":
            _out(Q.search(store, a.query, a.repo, a.limit), a.json)
        elif a.cmd == "sym":
            _out(Q.find_symbol(store, a.name, a.repo), a.json)
        elif a.cmd == "callers":
            s = _one_symbol(store, a.name, a.repo)
            _out({"symbol": s["qualname"], "at": f"{s['path']}:{s['start_line']}",
                  "callers": Q.callers(store, s["id"])}, a.json)
        elif a.cmd == "callees":
            s = _one_symbol(store, a.name, a.repo)
            _out({"symbol": s["qualname"], "calls": Q.callees(store, s["id"])}, a.json)
        elif a.cmd == "blast":
            s = _one_symbol(store, a.name, a.repo)
            _out(Q.blast_radius(store, s["id"], a.depth), a.json)
        elif a.cmd == "file":
            _out(Q.file_card(store, a.path, a.repo), a.json)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
