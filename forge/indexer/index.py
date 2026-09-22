"""Indexer orchestration — crawl, parse, store, then resolve the graph.

Two passes, deliberately. Pass one records every definition and every call site
as written. Pass two resolves those call sites to real symbol ids, which can
only happen once *all* files are known — a call on line 3 of the first file may
target a symbol defined in the last one.

Unresolved refs are left NULL rather than guessed. A wrong edge silently
misleads an engineer about blast radius; a missing edge is visible in
`resolved_pct` and can be reasoned about.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .languages import MAX_FILE_BYTES, lang_for, looks_generated, skip_dir
from .parse import parse_source
from .store import Store

# Extension candidates when a TS/JS import omits one, in resolution order.
_TS_EXTS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_TS_INDEX = tuple(f"/index{e}" for e in _TS_EXTS)


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _candidate_files(root: Path, git_only: bool = False) -> list[str]:
    """Repo-relative paths worth parsing.

    Default is a filesystem crawl filtered by SKIP_DIRS, *not* `git ls-files`.
    Two reasons, both learned from real trees:

    1. Anything the engineer just wrote is untracked. A tracked-only listing
       makes the loop's own new files invisible to its own index.
    2. .gitignore hides real source, not only build output — this platform
       keeps a working app under a gitignored `data/` path. SKIP_DIRS already
       excludes the genuine junk (node_modules, dist, .next, __pycache__).

    `git_only=True` is available for the case where the ignore rules really are
    the intended boundary.
    """
    if git_only and (root / ".git").exists():
        # --cached --others --exclude-standard = tracked + new, minus ignored
        listing = _git(root, "ls-files", "--cached", "--others",
                       "--exclude-standard", "-z")
        if listing:
            paths = [p for p in listing.split("\0") if p]
            return [p for p in paths
                    if lang_for(p) and not any(skip_dir(part)
                                               for part in Path(p).parts[:-1])]
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = str(full.relative_to(root)).replace(os.sep, "/")
            if lang_for(rel):
                out.append(rel)
    return out


def index_repo(root: str | Path, name: str | None = None,
               db_path: str | Path = "/data/forge.db",
               progress=None, git_only: bool = False) -> dict:
    """Index a working tree into the store. Returns a stats dict."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    name = name or root.name

    store = Store(db_path)
    head = _git(root, "rev-parse", "HEAD") or None
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD") or None
    repo_id = store.reset_repo(name, str(root), head, branch)

    paths = _candidate_files(root, git_only)
    if progress:
        progress(f"{name}: {len(paths)} source files to parse")

    file_ids: dict[str, int] = {}
    parse_errors = 0
    skipped_generated = 0
    for n, rel in enumerate(paths, 1):
        full = root / rel
        try:
            if full.stat().st_size > MAX_FILE_BYTES:
                continue
            src = full.read_bytes()
        except OSError:
            continue
        if looks_generated(src):
            skipped_generated += 1
            continue

        lang = lang_for(rel)
        parsed = parse_source(src, lang)
        if parsed.error:
            parse_errors += 1
        sha = hashlib.sha1(src).hexdigest()[:16]
        fid = store.add_file(repo_id, rel, lang, sha, parsed.loc, len(src), parsed.error)
        file_ids[rel] = fid
        store.add_symbols(repo_id, fid, parsed.symbols)
        store.add_imports(repo_id, fid, parsed.imports)
        store.add_refs(repo_id, fid, parsed.refs)
        store.add_literals(repo_id, fid, parsed.literals)

        if progress and n % 250 == 0:
            progress(f"  parsed {n}/{len(paths)}")
            store.commit()
    store.commit()

    if progress:
        progress("resolving graph edges")
    resolve(store, repo_id)

    stats = store.finalize(repo_id, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    stats.update({"repo": name, "repo_id": repo_id, "branch": branch,
                  "head": head, "parse_errors": parse_errors,
                  "skipped_generated": skipped_generated})
    store.close()
    return stats


# ─── pass two: resolution ───────────────────────────────────────────────────

def _resolve_module(module: str, from_path: str, by_path: dict[str, int]) -> int | None:
    """Best-effort module -> file id.

    Handles the three shapes that cover almost everything in this codebase's
    languages: TS relative paths, TS path aliases, and Python dotted modules.
    """
    if module.startswith("."):                        # ./x, ../y/z
        base = os.path.normpath(os.path.join(os.path.dirname(from_path), module))
        base = base.replace(os.sep, "/").lstrip("./")
        for cand in (base, *(base + e for e in _TS_EXTS), *(base + i for i in _TS_INDEX)):
            if cand in by_path:
                return by_path[cand]
        return None

    if module.startswith("@/") or module.startswith("~/"):   # bundler alias
        tail = module[2:]
        for cand in (*(tail + e for e in _TS_EXTS), *(tail + i for i in _TS_INDEX)):
            hit = _suffix_match(cand, by_path)
            if hit is not None:
                return hit
        return None

    if "." in module and "/" not in module:           # python dotted module
        tail = module.replace(".", "/")
        for cand in (tail + ".py", tail + "/__init__.py", tail + ".pyi"):
            hit = _suffix_match(cand, by_path)
            if hit is not None:
                return hit
        return None

    # bare package name (node_modules or stdlib) — out of tree, not an error
    return None


def _suffix_match(suffix: str, by_path: dict[str, int]) -> int | None:
    """Unique file whose path ends with `suffix`. Ambiguity resolves to None —
    two plausible targets means we genuinely don't know which one it is."""
    hit = None
    for path, fid in by_path.items():
        if path == suffix or path.endswith("/" + suffix):
            if hit is not None:
                return None
            hit = fid
    return hit


def resolve(store: Store, repo_id: int) -> None:
    db = store.db

    # 1. A ref's source: the symbol that encloses the call site.
    db.execute(
        "UPDATE refs SET src_symbol_id = ("
        "  SELECT s.id FROM symbols s"
        "  WHERE s.file_id = refs.file_id AND s.qualname = refs.src_qual)"
        " WHERE repo_id = ? AND src_qual IS NOT NULL", (repo_id,))

    by_path = {r["path"]: r["id"]
               for r in db.execute("SELECT id, path FROM files WHERE repo_id=?", (repo_id,))}
    path_of = {v: k for k, v in by_path.items()}

    # 2. Import edges: module string -> file id.
    imp_rows = db.execute(
        "SELECT id, file_id, module FROM imports WHERE repo_id=?", (repo_id,)).fetchall()
    updates = []
    cache: dict[tuple[str, str], int | None] = {}
    for r in imp_rows:
        frm = path_of.get(r["file_id"], "")
        key = (r["module"], os.path.dirname(frm))
        if key not in cache:
            cache[key] = _resolve_module(r["module"], frm, by_path)
        if cache[key] is not None:
            updates.append((cache[key], r["id"]))
    if updates:
        db.executemany("UPDATE imports SET resolved_file_id=? WHERE id=?", updates)

    # 3. Call edges. Three tiers, most specific first.
    sym_rows = db.execute(
        "SELECT id, file_id, name FROM symbols WHERE repo_id=?", (repo_id,)).fetchall()
    in_file: dict[tuple[int, str], int] = {}
    global_name: dict[str, list[int]] = {}
    for s in sym_rows:
        in_file.setdefault((s["file_id"], s["name"]), s["id"])
        global_name.setdefault(s["name"], []).append(s["id"])

    # What each file imports, by the local name the importer uses. Bindings to
    # modules we could NOT resolve matter just as much as resolved ones: they
    # prove the name refers to something outside the repo, which stops the
    # repo-wide fallback from inventing an edge (pydantic's `Field` is not the
    # `Field` class that happens to live in this codebase).
    imported: dict[tuple[int, str], int] = {}
    bound_external: set[tuple[int, str]] = set()
    for r in db.execute(
        "SELECT file_id, symbol, alias, resolved_file_id FROM imports"
        " WHERE repo_id=?", (repo_id,)):
        local = r["alias"] or r["symbol"]
        if not local or local == "*":
            continue
        if r["resolved_file_id"] is None:
            bound_external.add((r["file_id"], local))
            continue
        target = in_file.get((r["resolved_file_id"], r["symbol"]))
        if target is not None:
            imported[(r["file_id"], local)] = target

    ref_rows = db.execute(
        "SELECT id, file_id, name, full FROM refs WHERE repo_id=?", (repo_id,)).fetchall()
    edges = []
    for r in ref_rows:
        fid, nm = r["file_id"], r["name"]
        dst = in_file.get((fid, nm)) or imported.get((fid, nm))
        if dst is None and (fid, nm) not in bound_external:
            # The repo-wide fallback is only safe for a BARE call. For a member
            # call the receiver carries the meaning, and the bare name alone is
            # far too weak — that is how `datetime.now()` ends up pointing at a
            # local helper called `now`.
            if "." not in (r["full"] or ""):
                hits = global_name.get(nm)
                if hits and len(hits) == 1:
                    dst = hits[0]
        if dst is not None:
            edges.append((dst, r["id"]))
    if edges:
        db.executemany("UPDATE refs SET dst_symbol_id=? WHERE id=?", edges)
    db.commit()
