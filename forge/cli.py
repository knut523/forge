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
import shutil
import sys

from .indexer.index import index_repo
from .indexer import query as Q
from .indexer import wayfinder as W
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


def _render_impacts(rep: dict) -> str:
    """Impact reports lead with the cross-repo finding — that is the part a
    generic key/value dump buries, and the only part that is news."""
    t = rep["target"]
    what = t["symbol"] or t["path"]
    L = [f"TARGET   {t['repo']}  {what}",
         f"         {t['match_count']} symbol(s) in scope, "
         f"{len(rep['local_callers'])} caller(s) inside {t['repo']}",
         f"INDEXED  {', '.join(rep['indexed_repos'])}", ""]

    if not rep["cross_repo"]:
        L.append("CROSS-REPO   no evidence of impact outside this repo.")
    for e in rep["cross_repo"]:
        L.append(f"CROSS-REPO   {e['repo']}   [{e['confidence'].upper()}]")
        for sig, items in e["signals"].items():
            L.append(f"  via {sig}:")
            for i in items:
                if sig == "route":
                    L.append(f"    {i['confidence']:<6} {i['file']}:{i['line']}")
                    L.append(f"           calls {i['their_path']}  ->  our {i['our_path']}")
                else:
                    L.append(f"    {i['confidence']:<6} {i['file']}:{i['line']}  "
                             f"{i['name']} — {i['what']}")
        L.append("")
    for n in rep["notes"]:
        L.append(f"note: {n}")
    return "\n".join(L)


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

    p = sub.add_parser("org", help="list a GitHub org's repos (read-only)")
    p.add_argument("org")
    p.add_argument("--forks", action="store_true")
    p.add_argument("--archived", action="store_true")

    p = sub.add_parser("index-org", help="clone + index many repos from an org")
    p.add_argument("org")
    p.add_argument("--select", default="", help="comma-separated repo names; omit for --all")
    p.add_argument("--all", action="store_true", help="index every repo in the org")
    p.add_argument("--workdir", default="/data/repos")
    p.add_argument("--limit", type=int, default=25, help="safety cap on repo count")
    p.add_argument("--forks", action="store_true")
    p.add_argument("--archived", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep checkouts after indexing")

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

    p = sub.add_parser("impacts", help="cross-repo impact report (wayfinder)")
    p.add_argument("target", help="symbol name, or a file path with --file")
    p.add_argument("--file", action="store_true", help="treat target as a file path")

    sub.add_parser("links", help="which indexed repos look coupled")

    a = ap.parse_args(argv)

    if a.cmd == "index":
        stats = index_repo(a.path, a.name, a.db,
                           progress=lambda m: print(m, flush=True),
                           git_only=a.git_only)
        _out(stats, a.json)
        return 0

    if a.cmd in ("org", "index-org"):
        from .indexer import github as GH
        tok = GH.read_token(a.org)
        try:
            repos = GH.list_org_repos(a.org, tok, include_forks=a.forks,
                                      include_archived=a.archived)
        except GH.GitHubError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if not tok:
            print("note: no $FORGE_READ_TOKEN set — public repos only, "
                  "60 requests/hour", file=sys.stderr)

        if a.cmd == "org":
            _out([{k: r[k] for k in ("full_name", "private", "default_branch",
                                     "language", "size_kb", "pushed_at")}
                  for r in repos], a.json)
            return 0

        wanted = [s.strip() for s in a.select.split(",") if s.strip()]
        if wanted:
            missing = [w for w in wanted if w not in {r["name"] for r in repos}]
            if missing:
                print(f"error: not in {a.org}: {', '.join(missing)}", file=sys.stderr)
                return 2
            repos = [r for r in repos if r["name"] in wanted]
        elif not a.all:
            print("error: pass --select a,b,c or --all", file=sys.stderr)
            return 2

        if len(repos) > a.limit:
            print(f"error: {len(repos)} repos exceeds --limit {a.limit}. Raise it "
                  f"deliberately — cloning an org is a lot of disk and time.",
                  file=sys.stderr)
            return 2

        results, failed = [], []
        for i, r in enumerate(repos, 1):
            print(f"[{i}/{len(repos)}] {r['full_name']} …", flush=True)
            try:
                path = GH.clone(r, a.workdir, tok)
            except GH.GitHubError as e:
                print(f"  clone failed: {e}", file=sys.stderr)
                failed.append({"repo": r["full_name"], "error": str(e)[:200]})
                continue
            try:
                st = index_repo(path, r["name"], a.db,
                                progress=lambda m: print("  " + m, flush=True))
                results.append(st)
            except Exception as e:                      # one bad repo must not
                failed.append({"repo": r["full_name"],  # abort the whole sweep
                               "error": f"{type(e).__name__}: {str(e)[:200]}"})
            finally:
                if not a.keep:
                    shutil.rmtree(path, ignore_errors=True)
        _out({"indexed": results, "failed": failed,
              "ok": len(results), "errors": len(failed)}, a.json)
        return 1 if failed and not results else 0

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
        elif a.cmd == "impacts":
            if not a.repo:
                print("error: --repo is required for impacts (which repo owns "
                      "the change?)", file=sys.stderr)
                return 2
            rep = W.impacts(store, a.repo,
                            path=a.target if a.file else None,
                            symbol=None if a.file else a.target)
            print(json.dumps(rep, indent=2, default=str) if a.json
                  else _render_impacts(rep))
        elif a.cmd == "links":
            _out(W.repo_links(store), a.json)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
