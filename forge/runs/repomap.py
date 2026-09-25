"""Graph-ranked cross-file retrieval — Aider's repo-map, ported onto forge's index.

The ad-hoc `_crossfile_map` just listed a symbol's direct callers. Aider does better: it
treats the repo as a symbol-reference graph and runs *personalized* PageRank biased toward
the code under review, so a reviewer sees the code that is structurally most related to the
change — the other side of a cross-file invariant — ranked and budget-trimmed.

forge already has the graph (the `refs` table: which file references which symbol, and
`symbols` says which file defines it), so this is a small port: build the file→file
reference graph, bias the restart vector toward the PR's changed files/symbols, power-iterate
PageRank (no networkx dependency — ~20 lines), then render the top-ranked definitions.

Biasing follows Aider: identifiers mentioned in the change get ×10 edge weight, changed
files get ×50 on their out-edges, over-common symbols ×0.1, ref counts dampened by sqrt.
Read-only: pure SQL over the index.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

from ..indexer import query as Q


def _changed_idents(diff: str, cap: int = 60) -> set[str]:
    out: set[str] = set()
    for ln in (diff or "").splitlines():
        if not (ln.startswith("+") or ln.startswith("-")) or ln.startswith(("+++", "---")):
            continue
        for m in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", ln):
            out.add(m)
            if len(out) >= cap * 4:
                break
    return out


def _pagerank(nodes, out_edges, pers, alpha=0.85, iters=40):
    """Personalized PageRank by power iteration. `pers` is the (normalized) restart vector;
    dangling nodes redistribute to `pers` (Aider's trick — else rank leaks to the void)."""
    if not nodes:
        return {}
    s = sum(pers.values()) or 1.0
    pers = {n: pers.get(n, 0.0) / s for n in nodes}
    rank = {n: pers[n] for n in nodes}
    for _ in range(iters):
        new = {n: (1 - alpha) * pers[n] for n in nodes}
        dangling = 0.0
        for src in nodes:
            edges = out_edges.get(src)
            if not edges:
                dangling += rank[src]
                continue
            tw = sum(w for _, w in edges)
            r = alpha * rank[src]
            for dst, w in edges:
                new[dst] += r * (w / tw)
        if dangling:
            for n in nodes:
                new[n] += alpha * dangling * pers[n]
        rank = new
    return rank


def rank_context(store, repo_name: str, changed_files: list[str], diff: str,
                 max_defs: int = 14) -> str:
    """Return a graph-ranked list of the definitions most related to the change — the
    'read the USE sites too' context, chosen by structural centrality, not guesswork."""
    if store is None:
        return ""
    try:
        rid = Q._repo_id(store, repo_name)
    except Exception:
        return ""
    # refs: file_id (referencer) references dst_symbol_id (defined in some file). Join to
    # find the definer file + build the file→file edges, weighted per Aider.
    rows = store.db.execute(
        "SELECT r.name AS ident, r.file_id AS referencer, s.file_id AS definer,"
        "       s.id AS sym_id, s.qualname AS qual, s.kind AS kind, df.path AS defpath,"
        "       rf.path AS refpath"
        "  FROM refs r JOIN symbols s ON s.id = r.dst_symbol_id"
        "  JOIN files df ON df.id = s.file_id"
        "  JOIN files rf ON rf.id = r.file_id"
        " WHERE r.repo_id=? AND r.dst_symbol_id IS NOT NULL", (rid,)).fetchall()
    if not rows:
        return ""
    changed = set(changed_files or [])
    idents = _changed_idents(diff)
    # Neighbourhood of the change: file ids that directly reference, or are referenced by, a
    # changed file. Ranking is restricted to this 1-hop set so we surface the code coupled to
    # the change — not the repo's globally-central UI (pure PageRank leaks rank to those).
    changed_ids = {r["referencer"] for r in rows if r["refpath"] in changed} | \
                  {r["definer"] for r in rows if r["defpath"] in changed}
    neighbourhood = set(changed_ids)
    for r in rows:
        if r["referencer"] in changed_ids:
            neighbourhood.add(r["definer"])
        if r["definer"] in changed_ids:
            neighbourhood.add(r["referencer"])
    # count defs per ident (to down-weight over-common symbols)
    defcount: dict[str, int] = defaultdict(int)
    seen_def = set()
    for r in rows:
        k = (r["ident"], r["definer"])
        if k not in seen_def:
            seen_def.add(k)
            defcount[r["ident"]] += 1
    # aggregate edge weights referencer_file -> definer_file, and remember the tag
    edgew: dict[tuple, float] = defaultdict(float)
    edgecnt: dict[tuple, int] = defaultdict(int)
    tag_of: dict[tuple, tuple] = {}
    nodes: set = set()
    for r in rows:
        ref, dfn, ident = r["referencer"], r["definer"], r["ident"]
        if ref == dfn:
            continue
        nodes.add(ref); nodes.add(dfn)
        edgecnt[(ref, dfn, ident)] += 1
        tag_of[(dfn, ident)] = (r["defpath"], r["qual"] or r["ident"], r["kind"])
    for (ref, dfn, ident), c in edgecnt.items():
        mul = 1.0
        if ident in idents:
            mul *= 10.0
        if defcount.get(ident, 1) > 5:
            mul *= 0.1
        edgew[(ref, dfn)] += mul * math.sqrt(c)
    # personalize toward changed files (their symbols are the review's focus)
    path_by_id = {}
    for r in rows:
        path_by_id[r["referencer"]] = r["refpath"]
        path_by_id[r["definer"]] = r["defpath"]
    pers = {}
    for nid in nodes:
        p = path_by_id.get(nid, "")
        if p in changed:
            pers[nid] = 50.0            # the changed files are the restart focus
    if not pers:                        # nothing matched — fall back to uniform
        pers = {n: 1.0 for n in nodes}
    out_edges: dict = defaultdict(list)
    for (ref, dfn), w in edgew.items():
        out_edges[ref].append((dfn, w))
    rank = _pagerank(list(nodes), out_edges, pers)
    # distribute a node's rank across its out-edges onto (definer, ident) tags
    tagrank: dict[tuple, float] = defaultdict(float)
    for (ref, dfn, ident), c in edgecnt.items():
        w = edgew.get((ref, dfn), 0.0)
        if w <= 0:
            continue
        tw = sum(x for _, x in out_edges.get(ref, [])) or 1.0
        share = rank.get(ref, 0.0) * (w / tw)
        tagrank[(dfn, ident)] += share
    top = sorted(tagrank.items(), key=lambda kv: kv[1], reverse=True)
    lines, seen = [], set()
    for (dfn, ident), _score in top:
        tg = tag_of.get((dfn, ident))
        if not tg:
            continue
        path, qual, kind = tg
        if path in changed:            # already in the diff — the reviewer sees it
            continue
        if dfn not in neighbourhood:   # keep only code coupled to the change, not central noise
            continue
        key = (path, qual)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {path}: {kind or 'symbol'} `{qual}`")
        if len(lines) >= max_defs:
            break
    if not lines:
        return ""
    return ("GRAPH-RANKED RELATED CODE (the most structurally-connected definitions to this "
            "change — read these USE/DEF sites to check cross-file invariants):\n"
            + "\n".join(lines))
