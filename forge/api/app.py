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
from ..indexer import prs as PRs
from ..runs import engine as RunEngine
from ..runs.memory import KINDS, MemoryStore, remember_rejection
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


@app.post("/api/runs/{run_id}/approve")
def run_approve(run_id: str):
    """The gate. Only a run that is actually waiting on a human can pass it."""
    rs = RunStore()
    try:
        r = rs.get(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such run")
        if r["status"] != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"this run is {r['status']}, not waiting for approval")
        rs.emit(run_id, "approve", "done", "Approved by a human — opening the PR")
        rs.db.execute("UPDATE runs SET status='running', phase='pr' WHERE id=?",
                      (run_id,))
        rs.db.commit()
    finally:
        rs.close()
    RunEngine.approve(run_id)
    return {"approved": run_id}


class RejectIn(BaseModel):
    note: str | None = None


@app.post("/api/runs/{run_id}/reject")
def run_reject(run_id: str, body: RejectIn | None = None):
    rs = RunStore()
    try:
        r = rs.get(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such run")
        if r["status"] != "awaiting_approval":
            raise HTTPException(status_code=409,
                                detail=f"this run is {r['status']}")
        note = (body.note if body else None) or "no reason given"
        rs.emit(run_id, "approve", "warn", "Rejected by a human", {"note": note},
                level="warn")
        # The reason a human said no is the highest-value sentence this system
        # ever sees. It used to be discarded; now it becomes a lesson.
        mem = MemoryStore()
        try:
            remember_rejection(mem, r["repo"], run_id, r["goal"], note)
        finally:
            mem.close()
        rs.finish(run_id, "rejected",
                  reason=f"Rejected: {note}",
                  next_action="The workspace is kept — start a new run with a "
                              "sharper goal, or fix it by hand.")
    finally:
        rs.close()
    return {"rejected": run_id}


@app.delete("/api/runs/{run_id}")
def run_delete(run_id: str):
    rs = RunStore()
    try:
        if not rs.delete(run_id):
            raise HTTPException(status_code=404, detail="no such run")
        return {"deleted": run_id}
    finally:
        rs.close()


# ─── repo memory ────────────────────────────────────────────────────────────

class MemoryIn(BaseModel):
    repo: str
    kind: str
    title: str
    body: str


@app.get("/api/memory")
def memory_list(repo: str | None = None):
    m = MemoryStore()
    try:
        return {"kinds": list(KINDS), "memories": m.list(repo)}
    finally:
        m.close()


@app.post("/api/memory")
def memory_add(body: MemoryIn):
    m = MemoryStore()
    try:
        return m.add(body.repo, body.kind, body.title, body.body, source="human")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        m.close()


@app.delete("/api/memory/{mem_id}")
def memory_delete(mem_id: int):
    m = MemoryStore()
    try:
        if not m.delete(mem_id):
            raise HTTPException(status_code=404, detail="no such memory")
        return {"deleted": mem_id}
    finally:
        m.close()


@app.get("/api/memory/preview")
def memory_preview(repo: str):
    """Exactly what gets injected into the next run's prompts, verbatim."""
    m = MemoryStore()
    try:
        return {"repo": repo, "text": m.for_prompt(repo)}
    finally:
        m.close()


# ─── pull requests ──────────────────────────────────────────────────────────

def _read_token() -> str | None:
    c = _cfg()
    try:
        return c.get_secret(GITHUB_READ)
    finally:
        c.close()


def _indexed_origins() -> dict[str, str]:
    """GitHub full_name -> local index name, for the repos we can reason about."""
    store = _store()
    try:
        return {r["origin"]: r["name"] for r in Q.list_repos(store) if r.get("origin")}
    finally:
        store.close()


@app.get("/api/prs")
def prs_list(repos: str | None = None):
    """Open PRs across the indexed repos (or an explicit owner/repo list)."""
    token = _read_token()
    origins = _indexed_origins()
    wanted = [s.strip() for s in (repos or "").split(",") if s.strip()] or list(origins)
    if not token:
        return {"authenticated": False, "prs": [], "errors": [], "repos": wanted,
                "why": "No GitHub READ token set. Add one in Settings — listing "
                       "pull requests needs it, and only the read token is used."}
    if not wanted:
        return {"authenticated": True, "prs": [], "errors": [], "repos": [],
                "why": "None of the indexed repos has a GitHub origin. Index a "
                       "repo cloned from GitHub, or pass ?repos=owner/name."}
    items, errs = PRs.list_open(wanted, token)
    for p in items:
        p["indexed_as"] = origins.get(p["repo"])
    return {"authenticated": True, "prs": items, "errors": errs, "repos": wanted}


@app.get("/api/prs/{owner}/{repo}/{number}")
def pr_detail(owner: str, repo: str, number: int):
    token = _read_token()
    if not token:
        raise HTTPException(status_code=400, detail="no GitHub read token set")
    try:
        d = PRs.detail(owner, repo, number, token)
    except PRs.GHError as e:
        raise HTTPException(status_code=502, detail=str(e))
    store = _store()
    try:
        local = _indexed_origins().get(f"{owner}/{repo}")
        d["impact"] = PRs.impact_for(d, store, local)
    finally:
        store.close()
    return d


@app.get("/")
def index():
    f = UI / "index.html"
    if not f.exists():
        return JSONResponse({"error": "ui not built into this image"}, status_code=500)
    return FileResponse(f, headers={"Cache-Control": "no-store"})
