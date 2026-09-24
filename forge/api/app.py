"""forge HTTP API + UI host.

Deliberately small. It opens the SQLite index read-only per request, serves a
single static page, and does nothing else — no auth of its own (the edge owns
that), no background work, no platform dependencies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..config import providers as P
from ..config import llm
from ..config.store import (GITHUB_READ, GITHUB_WRITE, ConfigStore,
                            org_read_key, org_write_key)
from ..indexer import query as Q
from ..indexer import wayfinder as W
from ..indexer.store import Store
from ..indexer import prs as PRs
from ..runs import engine as RunEngine
from ..runs import epic as EpicEngine
from ..runs import pr_review as PRReview
from ..runs import github_write as GHWrite
from ..runs.memory import KINDS, MemoryStore, remember_rejection
from ..runs.store import EPIC_PHASES, IMPLEMENTED, PHASES, RunStore

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


class ChatIn(BaseModel):
    messages: list[dict] = []
    model: str | None = None
    repo: str | None = None


@app.post("/api/chat")
def chat(body: ChatIn):
    """Grounded multi-turn chat in the forge UI. Unlike a naked LLM, it retrieves
    matching code, past review comments and memory from forge's index and answers with
    that context (best-effort; falls back to a plain answer). Model-agnostic — works on
    the Claude bridge or any configured model."""
    from ..runs import chat as Chat
    c = _cfg()
    store = None
    try:
        models = [m for m in c.list_models() if m["enabled"]]
        if not models:
            raise HTTPException(status_code=400,
                                detail="no model configured — add one in Settings")
        chosen = (next((m for m in models if m["name"] == body.model), None)
                  or next((m for m in models if m["provider"] == "claude-cli"), None)
                  or models[0])
        token = c.get_secret(chosen["secret_key"]) if chosen["secret_key"] else None
        if chosen["secret_key"] and token is None:
            raise HTTPException(status_code=400,
                                detail=f"credential {chosen['secret_key']!r} is not set")
        try:
            store = _store()
        except Exception:
            store = None
        out = Chat.answer(c, store, body.messages or [], chosen, token, repo=body.repo)
        if out.get("text") is None:
            raise HTTPException(status_code=502,
                                detail=f"the model failed: {out.get('error')}")
        return {"text": out["text"], "model": out["model"], "grounded": out["grounded"]}
    finally:
        if store is not None:
            store.close()
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


class EpicIn(BaseModel):
    goal: str
    repos: list[str]


@app.post("/api/epics")
def epic_create(body: EpicIn):
    """A feature: framed, split into minimal PRs, then gated as a SPLIT."""
    store = _store()
    try:
        known = {r["name"] for r in Q.list_repos(store)}
    finally:
        store.close()
    bad = [r for r in body.repos if r not in known]
    if bad or not body.repos:
        raise HTTPException(status_code=400,
                            detail=f"not indexed: {', '.join(bad) or '(none given)'}")
    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="goal is required")
    rs = RunStore()
    try:
        eid = rs.create(body.repos[0], body.goal.strip(),
                        json.dumps(body.repos), None, kind="epic")
    finally:
        rs.close()
    EpicEngine.start(eid, DB)
    return {"id": eid}


_STOP = {"the", "and", "for", "with", "that", "this", "into", "from", "add",
         "make", "fix", "use", "should", "when", "where", "which", "your",
         "have", "will", "then", "than", "them", "they", "also", "each"}


class DetectIn(BaseModel):
    goal: str
    org: str | None = None


@app.post("/api/detect-repos")
def detect_repos(body: DetectIn):
    """Suggest which indexed repos a feature touches, by scoring each repo's
    symbols against the goal's keywords. Deterministic and model-free — the human
    confirms the picks before anything runs."""
    kws = [w for w in "".join(c.lower() if c.isalnum() else " "
                              for c in body.goal).split()
           if len(w) >= 4 and w not in _STOP][:12]
    store = _store()
    try:
        repos = [r for r in Q.list_repos(store)
                 if r.get("origin") and (not body.org
                                         or _org_of(r["origin"]) == body.org)]
        scored = []
        for r in repos:
            hits, samples = 0, []
            for kw in kws:
                res = Q.search(store, kw, r["name"], limit=5)
                hits += len(res)
                samples += [x.get("name") for x in res[:2] if x.get("name")]
            if hits:
                scored.append({"repo": r["name"], "origin": r["origin"],
                               "hits": hits,
                               "why": ", ".join(sorted(set(samples))[:5])})
    finally:
        store.close()
    scored.sort(key=lambda x: -x["hits"])
    return {"goal": body.goal, "keywords": kws, "suggested": scored[:6]}


@app.post("/api/epics/{epic_id}/advance")
def epic_advance(epic_id: str):
    """Kick an epic: start every item whose dependencies are done (idempotent —
    items already running are skipped). Lets you trigger builds modularly, e.g.
    the independent items in parallel, without a full re-decompose."""
    rs = RunStore()
    try:
        r = rs.get(epic_id)
        if r is None or r.get("kind") != "epic":
            raise HTTPException(status_code=404, detail="no such epic")
    finally:
        rs.close()
    EpicEngine.advance(epic_id, DB)
    return {"advanced": epic_id}


@app.post("/api/runs/{run_id}/rebuild")
def run_rebuild(run_id: str):
    """Re-run this item's build with the SAME goal under the current engine —
    used to bring an item built by an older flow up to the new one (review +
    tests) without touching the split. Preserves stack base and epic slot."""
    rs = RunStore()
    try:
        r = rs.get(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such run")
        if r["status"] not in ("awaiting_approval", "incomplete", "blocked", "failed"):
            raise HTTPException(status_code=409,
                                detail=f"this run is {r['status']}, cannot rebuild")
        new_id = rs.create(r["repo"], r["goal"], r.get("target"), None,
                           kind="run", parent_id=r.get("parent_id"))
        if r.get("base_branch"):
            rs.set_base(new_id, r["base_branch"])
        _ost = r.get("state")
        _ost = json.loads(_ost) if isinstance(_ost, str) else (_ost or {})
        if _ost.get("sibling_ctx"):                # keep a stacked item's dep files
            rs.set_state(new_id, {"sibling_ctx": _ost["sibling_ctx"]})
        rs.emit(run_id, "approve", "info", "Rebuilding under the current flow",
                {"new_run": new_id})
        rs.finish(run_id, "rejected", reason="Superseded by a rebuild.",
                  next_action="A fresh build was started under the current engine.")
        parent = r.get("parent_id")
        if parent:
            item = next((i for i in rs.items(parent) if i["run_id"] == run_id), None)
            if item:
                rs.set_item(item["id"], run_id=new_id, status="running")
    finally:
        rs.close()
    RunEngine.start(new_id, DB)
    return {"rebuilt": run_id, "new_run": new_id}


@app.get("/api/runs/{run_id}/items")
def run_items(run_id: str):
    rs = RunStore()
    try:
        return {"items": rs.items(run_id), "children": rs.children(run_id)}
    finally:
        rs.close()


@app.get("/api/phases")
def phases(kind: str = "run"):
    """The whole road, including the parts not built — so a short run is never
    mistaken for a complete one."""
    table = EPIC_PHASES if kind == "epic" else PHASES
    return [{"key": k, "title": t, "blurb": b,
             "implemented": kind == "epic" or k in IMPLEMENTED}
            for k, t, b in table]


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
        runs = rs.list(repo, limit)
        # tag each child with the goal of the epic it was split from, so a run is
        # never orphaned from the feature it belongs to
        goals = {}
        for pid in {r["parent_id"] for r in runs if r.get("parent_id")}:
            p = rs.get(pid)
            if p:
                goals[pid] = p["goal"]
        for r in runs:
            if r.get("parent_id"):
                r["parent_goal"] = goals.get(r["parent_id"])
        return runs
    finally:
        rs.close()


@app.get("/api/runs/{run_id}")
def run_get(run_id: str):
    rs = RunStore()
    try:
        r = rs.get(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such run")
        if r.get("parent_id"):
            p = rs.get(r["parent_id"])
            if p:
                r["parent_goal"] = p["goal"]
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
        is_epic = r.get("kind") == "epic"
        rs.emit(run_id, "approve", "done",
                "Split approved — running each pull request in turn" if is_epic
                else "Approved by a human — opening the PR")
        rs.db.execute("UPDATE runs SET status='running', phase=? WHERE id=?",
                      ("execute" if is_epic else "pr", run_id))
        rs.db.commit()
    finally:
        rs.close()
    if is_epic:
        EpicEngine.advance(run_id, DB)
    else:
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
        parent = r.get("parent_id")
    finally:
        rs.close()
    if parent:
        EpicEngine.on_child_finished(run_id, DB)
    return {"rejected": run_id}


@app.post("/api/runs/{run_id}/revise")
def run_revise(run_id: str, body: RejectIn | None = None):
    """Reviewer feedback → rebuild. Supersede this run with a fresh one whose goal
    carries the feedback, preserving its stack base and its place in an epic."""
    note = ((body.note if body else None) or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="revision needs feedback")
    rs = RunStore()
    try:
        r = rs.get(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such run")
        if r["status"] != "awaiting_approval":
            raise HTTPException(status_code=409, detail=f"this run is {r['status']}")
        mem = MemoryStore()
        try:
            remember_rejection(mem, r["repo"], run_id, r["goal"], note)
        finally:
            mem.close()
        new_goal = (r["goal"] + "\n\nRevision requested by the reviewer — address "
                    f"this precisely and keep everything else:\n{note}")
        new_id = rs.create(r["repo"], new_goal, r.get("target"), None,
                           kind="run", parent_id=r.get("parent_id"))
        if r.get("base_branch"):
            rs.set_base(new_id, r["base_branch"])
        _ost = r.get("state")
        _ost = json.loads(_ost) if isinstance(_ost, str) else (_ost or {})
        if _ost.get("sibling_ctx"):
            rs.set_state(new_id, {"sibling_ctx": _ost["sibling_ctx"]})
        rs.emit(run_id, "approve", "warn", "Revise requested — rebuilding with feedback",
                {"note": note, "new_run": new_id}, level="warn")
        rs.finish(run_id, "rejected", reason=f"Superseded by a revision: {note}",
                  next_action="A fresh run was started with your feedback.")
        parent = r.get("parent_id")
        if parent:
            item = next((i for i in rs.items(parent) if i["run_id"] == run_id), None)
            if item:
                rs.set_item(item["id"], run_id=new_id, status="running")
                rs.emit(parent, "execute", "info",
                        "Item revised with feedback — rebuilding",
                        {"item": item["seq"], "new_run": new_id})
    finally:
        rs.close()
    RunEngine.start(new_id, DB)
    return {"revised": run_id, "new_run": new_id}


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
def memory_preview(repo: str, touched: str | None = None):
    """What the next run would actually see — scoped to `touched` files/symbols when given
    (comma-separated), the full ranked set otherwise — plus any pin conflicts a human should
    reconcile. Read-only: unlike a real injection it does not bump hit counts."""
    tl = [t.strip() for t in (touched or "").split(",") if t.strip()]
    m = MemoryStore()
    try:
        rows = m.recall(repo, touched=tl or None)
        return {
            "repo": repo,
            "touched": tl,
            "count": len(rows),
            "memories": [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                          "body": r["body"], "pins": r.get("pins")} for r in rows],
            "conflicts": m.conflicts(repo),
        }
    finally:
        m.close()


# ─── pull requests ──────────────────────────────────────────────────────────

def _read_token(org: str | None = None) -> str | None:
    """The read token for an org (a GitHub owner). A per-org token wins; the
    global github_read_token is the fallback so a single-owner setup still works
    without any org config."""
    c = _cfg()
    try:
        if org:
            scoped = c.get_secret(org_read_key(org))
            if scoped:
                return scoped
        return c.get_secret(GITHUB_READ)
    finally:
        c.close()


def _org_of(full_name: str) -> str:
    return full_name.split("/", 1)[0]


def _indexed_origins() -> dict[str, str]:
    """GitHub full_name -> local index name, for the repos we can reason about."""
    store = _store()
    try:
        return {r["origin"]: r["name"] for r in Q.list_repos(store) if r.get("origin")}
    finally:
        store.close()


@app.get("/api/orgs")
def orgs_list():
    """The orgs forge knows about — one per GitHub owner across the indexed
    repos — and whether each has a usable read token. This is the axis the UI
    separates prometheus work from olaf work on."""
    origins = _indexed_origins()
    by_org: dict[str, list[str]] = {}
    for fn in origins:
        by_org.setdefault(_org_of(fn), []).append(fn)
    c = _cfg()
    try:
        has_global = c.get_secret(GITHUB_READ) is not None
        out = []
        for org in sorted(by_org):
            scoped = c.get_secret(org_read_key(org)) is not None
            out.append({
                "org": org,
                "repos": sorted(by_org[org]),
                "repo_count": len(by_org[org]),
                "token": "scoped" if scoped else ("global" if has_global else "none"),
                "token_key": org_read_key(org),
            })
    finally:
        c.close()
    return {"orgs": out, "has_global_token": has_global}


@app.get("/api/prs")
def prs_list(repos: str | None = None, org: str | None = None, fresh: int = 0):
    """Open PRs across the indexed repos, or one org, or an explicit list.

    Served from the local PR-history index by default (instant — the live GitHub
    fetch on every load is why the view felt like it reloaded). `?fresh=1` forces a
    live pull; POST /api/prs/reindex refreshes the index in the background.

    Repos are grouped by owner so each org is queried with its OWN read token —
    a prometheus token never touches an olaf repo and vice versa."""
    from ..runs import pr_history as PRHist
    origins = _indexed_origins()
    if repos:
        wanted = [s.strip() for s in repos.split(",") if s.strip()]
    else:
        wanted = [fn for fn in origins if not org or _org_of(fn) == org]

    if not fresh and wanted and PRHist.have(wanted):
        items = PRHist.open_prs(wanted)
        for p in items:
            p["indexed_as"] = origins.get(p["repo"])
        return {"authenticated": True, "prs": items, "errors": [], "repos": wanted,
                "orgs": sorted({_org_of(fn) for fn in wanted}), "source": "index"}

    if not wanted:
        why = (f"No indexed repo belongs to org {org!r}." if org and origins else
               "None of the indexed repos has a GitHub origin. Index a repo "
               "cloned from GitHub (forge index-org <owner>), or pass "
               "?repos=owner/name.")
        return {"authenticated": bool(_read_token(org)), "prs": [], "errors": [],
                "repos": [], "orgs": [], "why": why}

    groups: dict[str, list[str]] = {}
    for fn in wanted:
        groups.setdefault(_org_of(fn), []).append(fn)

    items: list[dict] = []
    errs: list[dict] = []
    missing: list[str] = []
    for owner, fns in sorted(groups.items()):
        token = _read_token(owner)
        if not token:
            missing.append(owner)
            errs.append({"repo": f"{owner}/*",
                         "error": f"no read token for org {owner!r} — add one in "
                                  f"Settings under key {org_read_key(owner)}"})
            continue
        got, gerr = PRs.list_open(fns, token)
        items += got
        errs += gerr
    items.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    for p in items:
        p["indexed_as"] = origins.get(p["repo"])

    resp = {"authenticated": len(missing) < len(groups), "prs": items,
            "errors": errs, "repos": wanted, "orgs": sorted(groups)}
    if not items and missing:
        resp["why"] = ("No read token for: " + ", ".join(missing) +
                       ". Add a per-org token in Settings, or a global "
                       f"{GITHUB_READ}.")
    return resp


@app.post("/api/prs/reindex")
def prs_reindex(org: str | None = None):
    """Refresh the PR-history index from GitHub in the background (read-only). The
    view keeps serving the old index until this finishes, so it never blocks."""
    import threading
    from ..runs import pr_history as PRHist
    origins = _indexed_origins()
    wanted = [fn for fn in origins if not org or _org_of(fn) == org]

    def _run():
        for fn in wanted:
            tok = _read_token(_org_of(fn))
            if tok:
                try:
                    PRHist.ingest_repo(fn, tok)
                except Exception:
                    pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "reindexing": wanted}


@app.get("/api/prs/{owner}/{repo}/{number}")
def pr_detail(owner: str, repo: str, number: int):
    token = _read_token(owner)
    if not token:
        raise HTTPException(status_code=400,
                            detail=f"no read token for org {owner!r} — add one in "
                                   f"Settings under key {org_read_key(owner)}")
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


@app.post("/api/prs/{owner}/{repo}/{number}/review")
def pr_review(owner: str, repo: str, number: int, mode: str = "light"):
    """Start a PR review as a run and return its id. The review streams each step
    as an event (poll /runs/{id}/events) and delivers the draft as the final
    'result' event. Read-only: uses the org READ token + models, never a write
    token. mode=light is one pass; mode=full runs multi-model finders + an
    adversarial verify pass."""
    mode = "full" if mode == "full" else "light"
    store = _store()
    try:
        local = _indexed_origins().get(f"{owner}/{repo}")
    finally:
        store.close()
    rs = RunStore()
    try:
        rid = rs.create(local or f"{owner}/{repo}",
                        f"Review {owner}/{repo}#{number} · {mode}",
                        f"{owner}/{repo}#{number}", None, kind="review")
    finally:
        rs.close()
    PRReview.start(rid, DB, owner, repo, number, mode)
    return {"run_id": rid, "mode": mode}


class CommentIn(BaseModel):
    body: str


def _write_token(org: str | None = None) -> str | None:
    """The write token for an org — the ONLY place GITHUB_WRITE is read outside
    engine.ship_it, and only from the human-approved comment handler below.
    Per-org token wins; the global github_write_token is the fallback."""
    c = _cfg()
    try:
        if org:
            scoped = c.get_secret(org_write_key(org))
            if scoped:
                return scoped
        return c.get_secret(GITHUB_WRITE)
    finally:
        c.close()


@app.post("/api/prs/{owner}/{repo}/{number}/comment")
def pr_comment(owner: str, repo: str, number: int, body: CommentIn):
    """Post a review comment to a PR — forge's second write path.

    This is the approval gate: it runs only from this explicit call, posts exactly
    the body the human submitted (no regeneration), and is the single place the
    WRITE token is read for this path."""
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="comment body is empty")
    token = _write_token(owner)
    if not token:
        raise HTTPException(status_code=400,
                            detail=f"no write token for org {owner!r} — add one in "
                                   f"Settings under key {org_write_key(owner)} "
                                   f"(or a global {GITHUB_WRITE})")
    try:
        posted = GHWrite.post_comment(owner, repo, number, text, token)
    except GHWrite.WriteError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, **posted}


@app.get("/")
def index():
    f = UI / "index.html"
    if not f.exists():
        return JSONResponse({"error": "ui not built into this image"}, status_code=500)
    return FileResponse(f, headers={"Cache-Control": "no-store"})
