"""Epics — a feature, split into the smallest pull requests that will carry it.

One run produces one diff. A feature is not a bigger diff; it is several small
ones with an order between them. Forcing a feature through a single run gives an
unreviewable diff, a model working far past the context it is reliable in, and a
single approve/reject on work that is mostly right.

So the expensive human decision moves earlier: **you approve the split, once,
before any code exists.** Reviewing eight boundaries takes a minute; reviewing
eight finished pull requests takes an hour, and by then the mistakes are built.

Each item then executes as an ordinary run — its own build, its own verify, its
own diff, its own approval, its own PR. Nothing about the per-change safety is
weakened by doing several of them.
"""
from __future__ import annotations

import json
import threading
import traceback

from ..config.store import ConfigStore
from ..config import llm
from ..indexer import query as Q
from ..indexer import wayfinder as W
from ..indexer.store import Store as IndexStore
from . import engine as RunEngine
from .memory import MemoryStore
from .store import RunStore

# Beyond this a "single logical change" is not single any more. Not a hard
# rule the model must obey — a threshold Assess flags for a human.
MAX_FILES_PER_ITEM = 6
MAX_ITEMS = 12

DECOMPOSE_SYSTEM = (
    "You split a software feature into the SMALLEST pull requests that can "
    "deliver it. You are given real indexed facts about the repositories.\n\n"
    "Rules for the split, in priority order:\n"
    "1. Each item must be INDEPENDENTLY REVIEWABLE — one reviewer, one sitting, "
    "one logical change. If you cannot describe an item in a single sentence "
    "without 'and', split it.\n"
    "2. Each item must be INDEPENDENTLY REVERTIBLE — reverting it must not break "
    "what shipped before it.\n"
    "3. Anything that defines a SHARED CONTRACT (a type, a route, a schema, a "
    "field) must be its OWN item and must come FIRST, so later items build "
    "against it instead of each inventing their own version.\n"
    "4. Prefer more small items over fewer large ones. An item touching more "
    f"than {MAX_FILES_PER_ITEM} files is almost certainly two items.\n"
    "5. Do not invent work. If the feature is genuinely one small change, return "
    "ONE item and say so.\n\n"
    "Output ONLY a JSON object, no prose and no fences:\n"
    '{"items":[{"title":"imperative one-liner","repo":"<indexed repo name>",'
    '"rationale":"why this is its own PR","files":["likely/path.py"],'
    '"acceptance":["observable check"],"depends_on":[<0-based indices>],'
    '"risk":"low|medium|high"}],"notes":"how you split it and why"}'
)

ASSESS_SYSTEM = (
    "You review a proposed split of a feature into pull requests. You are a "
    "sceptic: your job is to find items that are too big, overlapping, or not "
    "independently shippable. For each item give a verdict of 'ok', 'split' "
    "(too large — say how to divide it) or 'merge' (too small to stand alone — "
    "say which item to fold it into). Be specific and brief. "
    'Output ONLY JSON: {"verdicts":[{"index":0,"verdict":"ok","why":"..."}],'
    '"overall":"one paragraph"}'
)


def _pick_model(cfg: ConfigStore):
    models = [m for m in cfg.list_models() if m["enabled"]]
    chosen = next((m for m in models if m["role"] == "engineer"), None)
    if chosen is None:
        return None, None, "no model has the 'engineer' role — set one in Settings"
    token = cfg.get_secret(chosen["secret_key"]) if chosen["secret_key"] else None
    if chosen["secret_key"] and token is None:
        return None, None, f"credential {chosen['secret_key']!r} is not set"
    return chosen, token, ""


def _frame(rs: RunStore, eid: str, idx: IndexStore, repos: list[str]) -> str:
    rs.set_phase(eid, "frame")
    lines = []
    for repo in repos:
        ov = Q.overview(idx, repo)
        hot = Q.hotspots(idx, repo, 6)
        rs.emit(eid, "frame", "metric", f"{repo}", {
            "files": ov["file_count"], "symbols": ov["symbol_count"],
            "languages": [l["lang"] for l in ov["languages"]]})
        lines.append(f"REPO {repo}: {ov['file_count']} files, "
                     f"{ov['symbol_count']} symbols, "
                     f"languages {', '.join(l['lang'] for l in ov['languages'])}")
        lines += [f"  load-bearing: {h['qualname']} ({h['refs']} refs) {h['path']}"
                  for h in hot]

    links = W.repo_links(idx)
    coupled = [l for l in links if l["from"] in repos]
    if coupled:
        rs.emit(eid, "frame", "impact", "These repos are coupled", coupled,
                level="warn")
        lines.append("COUPLING between repos (a change in one can reach another):")
        lines += [f"  {l['from']} -> {l['to']} via {l['routes']} shared paths"
                  for l in coupled]
    rs.emit(eid, "frame", "done", f"Framed across {len(repos)} repo(s)")
    return "\n".join(lines)


def _decompose(rs: RunStore, eid: str, cfg: ConfigStore, goal: str,
               facts: str, repos: list[str], recall: str) -> list[dict] | None:
    rs.set_phase(eid, "decompose")
    model, token, why = _pick_model(cfg)
    if model is None:
        rs.emit(eid, "decompose", "warn", "No engineer model — cannot split",
                {"reason": why}, level="warn")
        return None
    rs.emit(eid, "decompose", "step", f"Splitting with {model['name']}")
    text, meta = llm.complete(
        model, token, DECOMPOSE_SYSTEM,
        f"FEATURE\n{goal}\n\nINDEXED REPOSITORIES\n{facts}\n\n"
        f"AVAILABLE REPO NAMES (use exactly these): {', '.join(repos)}"
        + (f"\n\n{recall}" if recall else ""),
        max_tokens=8000)
    if text is None:
        rs.emit(eid, "decompose", "error", "The model call failed", meta, level="error")
        return None
    obj = RunEngine._extract_json(text) or {}
    items = obj.get("items")
    if not isinstance(items, list) or not items:
        rs.emit(eid, "decompose", "error", "No usable split came back",
                {"raw": text[:1200]}, level="error")
        return None
    items = items[:MAX_ITEMS]
    if obj.get("notes"):
        rs.emit(eid, "decompose", "info", "How it was split",
                {"text": str(obj["notes"])[:1500], **meta})
    rs.emit(eid, "decompose", "done", f"Split into {len(items)} pull request(s)")
    return items


def _assess(rs: RunStore, eid: str, cfg: ConfigStore, items: list[dict],
            repos: list[str]) -> dict:
    """Deterministic checks first, then a sceptic. Cheap and reliable before
    expensive and fallible."""
    rs.set_phase(eid, "assess")
    problems: list[dict] = []

    # Two items editing the same file cannot be reviewed or reverted
    # independently — this is the parallel-PR conflict, caught before it exists.
    claims: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        for f in it.get("files") or []:
            claims.setdefault(f"{it.get('repo')}:{f}", []).append(i)
    for path, owners in claims.items():
        if len(owners) > 1:
            problems.append({"kind": "overlap", "path": path, "items": owners,
                             "why": "more than one item edits this file"})

    for i, it in enumerate(items):
        n = len(it.get("files") or [])
        if n > MAX_FILES_PER_ITEM:
            problems.append({"kind": "too_big", "item": i, "files": n,
                             "why": f"{n} files — probably more than one change"})
        if not (it.get("acceptance") or []):
            problems.append({"kind": "unverifiable", "item": i,
                             "why": "no observable acceptance criterion"})
        if it.get("repo") not in repos:
            problems.append({"kind": "unknown_repo", "item": i,
                             "why": f"{it.get('repo')!r} is not an indexed repo"})
        for d in it.get("depends_on") or []:
            if not isinstance(d, int) or d >= len(items) or d == i:
                problems.append({"kind": "bad_dependency", "item": i,
                                 "why": f"depends_on {d} is not a valid earlier item"})

    if problems:
        rs.emit(eid, "assess", "warn", f"{len(problems)} structural problem(s) in the split",
                problems, level="warn")
    else:
        rs.emit(eid, "assess", "done",
                "No overlapping files, every item has a check, dependencies resolve")

    verdicts = []
    model, token, _ = _pick_model(cfg)
    if model is not None:
        summary = "\n".join(
            f"{i}. [{it.get('repo')}] {it.get('title')} — files: "
            f"{', '.join(it.get('files') or []) or 'none named'}"
            for i, it in enumerate(items))
        text, _meta = llm.complete(model, token, ASSESS_SYSTEM,
                                   f"PROPOSED SPLIT\n{summary}", max_tokens=3000)
        obj = RunEngine._extract_json(text or "") or {}
        verdicts = obj.get("verdicts") or []
        flagged = [v for v in verdicts if v.get("verdict") in ("split", "merge")]
        if flagged:
            rs.emit(eid, "assess", "finding",
                    f"The reviewer would change {len(flagged)} item(s)", flagged,
                    level="warn")
        if obj.get("overall"):
            rs.emit(eid, "assess", "info", "Reviewer's read",
                    {"text": str(obj["overall"])[:1500]})
    return {"problems": problems, "verdicts": verdicts}


def execute(epic_id: str, index_db: str) -> None:
    """Frame, split, assess — then stop and wait for a human to approve the split."""
    rs, cfg, mem = RunStore(), ConfigStore(), MemoryStore()
    idx = None
    try:
        epic = rs.get(epic_id)
        if epic is None:
            return
        repos = json.loads(epic["target"] or "[]") or [epic["repo"]]
        rs.emit(epic_id, None, "info", "Feature started",
                {"goal": epic["goal"], "repos": repos})
        idx = IndexStore(index_db, readonly=True)

        facts = _frame(rs, epic_id, idx, repos)
        recall = "\n\n".join(filter(None, (mem.for_prompt(r) for r in repos)))
        items = _decompose(rs, epic_id, cfg, epic["goal"], facts, repos, recall)
        if items is None:
            return rs.finish(epic_id, "blocked",
                             reason="The feature could not be split.",
                             next_action="Check Settings for an engineer model, "
                                         "then start it again.")
        assessment = _assess(rs, epic_id, cfg, items, repos)

        rs.clear_items(epic_id)
        by_index = {}
        for i, it in enumerate(items):
            by_index[i] = rs.add_item(epic_id, i, it)
        for i, it in enumerate(items):
            v = next((x for x in assessment["verdicts"]
                      if x.get("index") == i), None)
            probs = [p for p in assessment["problems"] if p.get("item") == i
                     or i in (p.get("items") or [])]
            rs.set_item(by_index[i], assessment={"verdict": v, "problems": probs})

        rs.set_state(epic_id, {"repos": repos, "facts": facts[:20000]})
        rs.set_phase(epic_id, "approve")
        rs.emit(epic_id, "approve", "info", "Waiting for you to approve the split",
                {"pull_requests": len(items),
                 "structural_problems": len(assessment["problems"]),
                 "note": "No code has been written. Approving runs each item as "
                         "its own change, and each one still stops for your "
                         "review before its PR is opened."})
        rs.finish(epic_id, "awaiting_approval",
                  reason=f"Split into {len(items)} pull request(s). Nothing built yet.",
                  next_action="Review the split below, then approve it.")
    except Exception as e:
        rs.emit(epic_id, None, "error", f"Failed: {type(e).__name__}",
                {"error": str(e)[:400], "trace": traceback.format_exc()[-1000:]},
                level="error")
        rs.finish(epic_id, "failed", error=str(e)[:200],
                  reason="The feature planning failed.")
    finally:
        if idx is not None:
            idx.close()
        mem.close()
        cfg.close()
        rs.close()


def start(epic_id: str, index_db: str) -> None:
    threading.Thread(target=execute, args=(epic_id, index_db), daemon=True).start()


# ─── execution, one pull request at a time ──────────────────────────────────

def advance(epic_id: str, index_db: str) -> None:
    """Start the next item whose dependencies are all done.

    Strictly one at a time. Parallelism is an optimisation; being unable to say
    which change broke something is not a trade worth making yet.
    """
    rs = RunStore()
    try:
        items = rs.items(epic_id)
        done = {i["seq"] for i in items if i["status"] == "complete"}
        running = [i for i in items if i["status"] == "running"]
        if running:
            return
        nxt = next((i for i in items
                    if i["status"] == "pending"
                    and all(d in done for d in i["depends_on"])), None)
        if nxt is None:
            left = [i for i in items if i["status"] not in ("complete", "skipped")]
            if left:
                rs.finish(epic_id, "blocked",
                          reason=f"{len(left)} item(s) cannot start — their "
                                 f"dependencies did not complete.",
                          next_action="Review the rejected or failed items below.")
            else:
                urls = [i for i in items if i["status"] == "complete"]
                rs.finish(epic_id, "complete",
                          reason=f"All {len(urls)} pull request(s) opened.",
                          next_action="Review them on GitHub.")
            return

        goal = (f"{nxt['title']}\n\nThis is one piece of a larger feature: "
                f"{rs.get(epic_id)['goal']}\n\nWhy this is its own change: "
                f"{nxt['rationale']}\n\nIt must satisfy: "
                + "; ".join(nxt["acceptance"]))
        child = rs.create(nxt["repo"], goal,
                          (nxt["files"] or [None])[0], None,
                          kind="run", parent_id=epic_id)
        rs.set_item(nxt["id"], status="running", run_id=child)
        rs.db.execute("UPDATE runs SET status='running', phase='execute' WHERE id=?",
                      (epic_id,))
        rs.db.commit()
        rs.emit(epic_id, "execute", "step",
                f"Item {nxt['seq'] + 1}/{len(items)}: {nxt['title']}",
                {"repo": nxt["repo"], "run": child, "files": nxt["files"]})
    finally:
        rs.close()
    RunEngine.start(child, index_db)


def on_child_finished(child_id: str, index_db: str) -> None:
    """Called when an item's run reaches a terminal state."""
    rs = RunStore()
    try:
        child = rs.get(child_id)
        if not child or not child.get("parent_id"):
            return
        epic_id = child["parent_id"]
        item = next((i for i in rs.items(epic_id) if i["run_id"] == child_id), None)
        if item is None:
            return
        status = {"complete": "complete", "rejected": "rejected"}.get(
            child["status"], "failed")
        rs.set_item(item["id"], status=status)
        rs.emit(epic_id, "execute",
                "done" if status == "complete" else "warn",
                f"Item {item['seq'] + 1} {status}"
                + (f" — {child.get('pr_url')}" if child.get("pr_url") else ""),
                level="info" if status == "complete" else "warn")
    finally:
        rs.close()
    if status == "complete":
        advance(epic_id, index_db)
    else:
        rs2 = RunStore()
        try:
            rs2.finish(epic_id, "blocked",
                       reason=f"Item {item['seq'] + 1} was {status}, so the rest "
                              f"of the feature is on hold.",
                       next_action="Fix or re-run that item, then continue.")
        finally:
            rs2.close()
