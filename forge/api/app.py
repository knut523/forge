"""forge HTTP API + UI host.

Deliberately small. It opens the SQLite index read-only per request, serves a
single static page, and does nothing else — no auth of its own (the edge owns
that), no background work, no platform dependencies.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from ..indexer import query as Q
from ..indexer import wayfinder as W
from ..indexer.store import Store

DB = os.environ.get("FORGE_DB", "/data/forge.db")
UI = Path(__file__).resolve().parent.parent / "ui"

app = FastAPI(title="forge", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _store() -> Store:
    return Store(DB, readonly=True)


def _call(fn, *a, **kw):
    """Run a query, turning 'not indexed' style errors into clean 4xx."""
    store = _store()
    try:
        return fn(store, *a, **kw)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        store.close()


@app.get("/api/health")
def health():
    store = _store()
    try:
        n = len(Q.list_repos(store))
    finally:
        store.close()
    return {"ok": True, "repos": n, "db": DB}


@app.get("/api/repos")
def repos():
    return _call(Q.list_repos)


@app.get("/api/overview")
def overview(repo: str | None = None):
    return _call(Q.overview, repo)


@app.get("/api/search")
def search(q: str, repo: str | None = None, limit: int = Query(30, le=200)):
    return _call(Q.search, q, repo, limit)


@app.get("/api/symbol")
def symbol(name: str, repo: str | None = None):
    return _call(Q.find_symbol, name, repo)


@app.get("/api/callers")
def callers(id: int, limit: int = Query(100, le=500)):
    return _call(Q.callers, id, limit)


@app.get("/api/callees")
def callees(id: int, limit: int = Query(100, le=500)):
    return _call(Q.callees, id, limit)


@app.get("/api/blast")
def blast(id: int, depth: int = Query(2, ge=1, le=5)):
    return _call(Q.blast_radius, id, depth)


@app.get("/api/file")
def file_card(path: str, repo: str | None = None):
    return _call(Q.file_card, path, repo)


@app.get("/api/hotspots")
def hotspots(repo: str | None = None, limit: int = Query(20, le=100)):
    return _call(Q.hotspots, repo, limit)


@app.get("/api/orphans")
def orphans(repo: str | None = None, limit: int = Query(50, le=200)):
    return _call(Q.orphans, repo, limit)


@app.get("/api/links")
def links():
    return _call(W.repo_links)


@app.get("/api/impacts")
def impacts(repo: str, symbol: str | None = None, path: str | None = None):
    if not symbol and not path:
        raise HTTPException(status_code=400, detail="pass symbol or path")
    return _call(W.impacts, repo, symbol, path)


@app.get("/")
def index():
    f = UI / "index.html"
    if not f.exists():
        return JSONResponse({"error": "ui not built into this image"}, status_code=500)
    return FileResponse(f, headers={"Cache-Control": "no-store"})
