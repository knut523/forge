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
from pydantic import BaseModel

from ..config import providers as P
from ..config.store import GITHUB_READ, GITHUB_WRITE, ConfigStore
from ..indexer import query as Q
from ..indexer import wayfinder as W
from ..indexer.store import Store
from ..runs import engine as RunEngine
from ..runs.store import IMPLEMENTED, PHASES, RunStore

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


# ─── credentials ────────────────────────────────────────────────────────────
# Values go in and are never handed back out: every response describes a secret
# (provider, last four characters, when it was set) but never reveals it.

class SecretIn(BaseModel):
    key: str
    value: str
    label: str | None = None
    provider: str | None = None


class ModelIn(BaseModel):
    name: str
    provider: str
    model_id: str
    base_url: str | None = None
    secret_key: str | None = None
    role: str | None = None


def _cfg() -> ConfigStore:
    try:
        return ConfigStore()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"config store: {e}")


@app.get("/api/settings")
def settings_list():
    c = _cfg()
    try:
        return c.list_secrets()
    finally:
        c.close()


@app.put("/api/settings")
def settings_put(body: SecretIn):
    c = _cfg()
    try:
        return c.set_secret(body.key.strip(), body.value,
                            body.label, body.provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        c.close()


@app.delete("/api/settings/{key}")
def settings_delete(key: str):
    c = _cfg()
    try:
        if not c.delete_secret(key):
            raise HTTPException(status_code=404, detail="no such setting")
        return {"deleted": key}
    finally:
        c.close()


@app.post("/api/settings/{key}/verify")
def settings_verify(key: str):
    """Probe the credential, and record the verdict alongside it."""
    c = _cfg()
    try:
        token = c.get_secret(key)
        if token is None:
            raise HTTPException(status_code=404, detail="not set")
        desc = c.describe(key) or {}
        provider = desc.get("provider") or ("github" if "github" in key else "")
        expect = "write" if key == GITHUB_WRITE else "read"
        res = P.verify(provider, token, expect=expect)
        c.set_meta(key, {"verified_at": desc.get("updated_at"), **res})
        return res
    finally:
        c.close()


# ─── models ─────────────────────────────────────────────────────────────────

@app.get("/api/models")
def models_list():
    c = _cfg()
    try:
        return c.list_models()
    finally:
        c.close()


@app.post("/api/models")
def models_add(body: ModelIn):
    c = _cfg()
    try:
        if body.secret_key and c.get_secret(body.secret_key) is None:
            raise HTTPException(
                status_code=400,
                detail=f"no credential named {body.secret_key!r} — add the key first")
        return c.add_model(body.name.strip(), body.provider, body.model_id.strip(),
                           body.base_url, body.secret_key, body.role)
    finally:
        c.close()


@app.delete("/api/models/{name}")
def models_delete(name: str):
    c = _cfg()
    try:
        if not c.delete_model(name):
            raise HTTPException(status_code=404, detail="no such model")
        return {"deleted": name}
    finally:
        c.close()


@app.post("/api/models/{name}/test")
def models_test(name: str):
    c = _cfg()
    try:
        m = c.get_model(name)
        if m is None:
            raise HTTPException(status_code=404, detail="no such model")
        token = c.get_secret(m["secret_key"]) if m["secret_key"] else None
        res = P.verify(m["provider"], token, m["base_url"])
        # reachability is not the same as "this model id exists" — say which
        if res.get("ok") and res.get("models") is not None:
            res["model_present"] = m["model_id"] in res["models"]
            if not res["model_present"]:
                res["warnings"] = list(res.get("warnings", [])) + [
                    f"provider reachable, but {m['model_id']!r} is not in its "
                    f"model list"]
        c.set_model_meta(name, res)
        return res
    finally:
        c.close()


@app.get("/api/providers/{provider}/models")
def provider_models(provider: str, secret_key: str | None = None,
                    base_url: str | None = None):
    """What this provider offers, so a model can be picked rather than typed."""
    c = _cfg()
    try:
        token = c.get_secret(secret_key) if secret_key else None
        res = P.verify(provider, token, base_url)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("detail"))
        return {"models": res.get("models", []), "detail": res.get("detail")}
    finally:
        c.close()


# ─── runs (the coder) ───────────────────────────────────────────────────────

class RunIn(BaseModel):
    repo: str
    goal: str
    target: str | None = None


@app.get("/api/phases")
def phases():
    """The whole road, including the parts not built — so a short run is never
    mistaken for a complete one."""
    return [{"key": k, "title": t, "blurb": b, "implemented": k in IMPLEMENTED}
            for k, t, b in PHASES]


@app.post("/api/runs")
def run_create(body: RunIn):
    store = _store()
    try:
        names = {r["name"] for r in Q.list_repos(store)}
    finally:
        store.close()
    if body.repo not in names:
        raise HTTPException(status_code=400,
                            detail=f"{body.repo!r} is not indexed")
    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="goal is required")

    rs = RunStore()
    try:
        cfg = ConfigStore()
        try:
            eng = next((m for m in cfg.list_models()
                        if m["role"] == "engineer" and m["enabled"]), None)
        finally:
            cfg.close()
        rid = rs.create(body.repo, body.goal.strip(),
                        (body.target or "").strip() or None,
                        eng["name"] if eng else None)
    finally:
        rs.close()
    RunEngine.start(rid, DB)
    return {"id": rid}


@app.get("/api/runs")
def run_list(repo: str | None = None, limit: int = Query(50, le=200)):
    rs = RunStore()
    try:
        return rs.list(repo, limit)
    finally:
        rs.close()


@app.get("/api/runs/{run_id}")
def run_get(run_id: str):
    rs = RunStore()
    try:
        r = rs.get(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such run")
        return r
    finally:
        rs.close()


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0):
    """Incremental: the view polls with the last seq it has, so a long run does
    not re-ship its whole history every second."""
    rs = RunStore()
    try:
        if rs.get(run_id) is None:
            raise HTTPException(status_code=404, detail="no such run")
        return rs.events(run_id, after)
    finally:
        rs.close()


@app.delete("/api/runs/{run_id}")
def run_delete(run_id: str):
    rs = RunStore()
    try:
        if not rs.delete(run_id):
            raise HTTPException(status_code=404, detail="no such run")
        return {"deleted": run_id}
    finally:
        rs.close()


@app.get("/")
def index():
    f = UI / "index.html"
    if not f.exists():
        return JSONResponse({"error": "ui not built into this image"}, status_code=500)
    return FileResponse(f, headers={"Cache-Control": "no-store"})
