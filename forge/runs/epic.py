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
import os
import threading
import traceback

from ..config.store import ConfigStore
from ..config import llm
from ..indexer import query as Q
from ..indexer import wayfinder as W
from ..indexer.store import Store as IndexStore
from . import engine as RunEngine
from . import council as Council
from .ship import branch_name
from .memory import MemoryStore
from .store import RunStore

# Beyond this a "single logical change" is not single any more. Not a hard
# rule the model must obey — a threshold Assess flags for a human.
MAX_FILES_PER_ITEM = 6
MAX_ITEMS = 12

DECOMPOSE_SYSTEM = (
    "You split a software feature into the FEWEST pull requests that still deliver "
    "it cleanly. Fewer, coherent PRs beat many fragments. Over-splitting is a "
    "defect: it makes more review, more CI, and dead-code PRs that ship nothing on "
    "their own. You are given real indexed facts about the repositories.\n\n"
    "Rules, in priority order:\n"
    "1. MINIMAL COUNT. Emit the smallest number of PRs that ships the feature. Do "
    "NOT split for its own sake. If it is genuinely one change, return ONE item.\n"
    "2. EACH ITEM DELIVERS VALUE ON ITS OWN. A PR must change product behaviour or "
    "a real integration by itself. A helper, type, or function and its ONLY caller "
    "in the same repo are ONE item — never ship code that nothing calls yet.\n"
    "3. NO INVESTIGATION PRs. Never create an item whose job is only to count, "
    "measure, report on, or size existing data. That is a query the operator runs, "
    "not a pull request.\n"
    "4. SHARED CONTRACT FIRST — but only when TWO OR MORE later items consume it. "
    "A type/route/schema/field used by >=2 items is its own first item; if only "
    "one item uses it, fold them together.\n"
    "5. CROSS-REPO: implement shared logic ONCE, in the repo where it actually "
    "runs. Duplicate it into another repo ONLY if that repo independently needs it "
    "at runtime — and then name that need in the rationale. Prefer one "
    "implementation plus a contract over N copies.\n"
    "6. Each item must still be INDEPENDENTLY REVIEWABLE (one reviewer, one "
    "sitting, one logical change — if you need 'and' to describe it, reconsider) "
    "and INDEPENDENTLY REVERTIBLE (reverting it does not break what shipped "
    "before).\n"
    f"7. An item touching more than {MAX_FILES_PER_ITEM} files is probably two — "
    "but do not split one coherent change just to lower the file count.\n\n"
    "Output ONLY a JSON object, no prose and no fences:\n"
    '{"items":[{"title":"imperative one-liner","repo":"<indexed repo name>",'
    '"rationale":"why this is its own PR and why it must ship separately",'
    '"files":["likely/path.py"],"acceptance":["observable check"],'
    '"depends_on":[<0-based indices>],"risk":"low|medium|high"}],'
    '"notes":"how few PRs you used and why that is the minimum"}'
)

ASSESS_SYSTEM = (
    "You review a proposed split of a feature into pull requests, as a sceptic "
    "whose goal is the FEWEST PRs that still ship it. Hunt sprawl. For each item "
    "return a verdict:\n"
    "- 'ok' — it earns its own PR (delivers product value by itself).\n"
    "- 'merge' — too small or dead on its own: a helper/type split from its only "
    "caller, or a shared contract with a single consumer. Give 'fold_into' = the "
    "index of the item it belongs with.\n"
    "- 'drop' — it should not be a PR at all: an investigation/measurement/"
    "reporting item, a cross-repo duplicate of logic that already ships elsewhere, "
    "or work the feature does not need.\n"
    "- 'split' — genuinely two changes; say how to divide.\n"
    "Prefer 'merge'/'drop' over 'ok' when an item does not deliver value alone. "
    "Be specific and brief.\n"
    'Output ONLY JSON: {"verdicts":[{"index":0,"verdict":"ok|merge|drop|split",'
    '"fold_into":<index or null>,"why":"..."}],"recommended_pr_count":<int>,'
    '"overall":"one paragraph"}'
)


def _apply_verdicts(items: list[dict],
                    verdicts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Actually shrink the split: fold 'merge' items into their target and remove
    'drop' items, then reindex depends_on onto the survivors. Fail-safe by design —
    on any inconsistency it returns the items unchanged, because a wrong reduction
    must never break a plan or delete real work."""
    try:
        vby = {v["index"]: v for v in verdicts if isinstance(v.get("index"), int)}
        n = len(items)
        fold: dict[int, int] = {}   # src -> direct target
        remove: set[int] = set()
        for i in range(n):
            v = vby.get(i)
            if not v:
                continue
            verd = str(v.get("verdict", "")).lower()
            if verd == "drop":
                remove.add(i)
            elif verd == "merge":
                dst = v.get("fold_into")
                if isinstance(dst, int) and 0 <= dst < n and dst != i:
                    fold[i] = dst
                    remove.add(i)
        if not remove:
            return items, []

        def final_dst(x: int) -> int:
            seen = set()
            while x in fold and x not in seen:
                seen.add(x)
                x = fold[x]
            return x

        survivors = [i for i in range(n) if i not in remove]
        if not survivors:                      # never drop the whole feature
            return items, []
        work = [dict(it) for it in items]      # do not mutate the caller's list
        for src in fold:
            dst = final_dst(src)
            if dst in remove:                  # fold target got removed → bail safe
                return items, []
            work[dst].setdefault("files", [])
            for f in work[src].get("files") or []:
                if f not in work[dst]["files"]:
                    work[dst]["files"].append(f)
            work[dst].setdefault("acceptance", [])
            for a in work[src].get("acceptance") or []:
                if a not in work[dst]["acceptance"]:
                    work[dst]["acceptance"].append(a)
        oldnew = {old: new for new, old in enumerate(survivors)}
        reduced = []
        for old in survivors:
            it = dict(work[old])
            deps = []
            for d in it.get("depends_on") or []:
                t = final_dst(d) if d in fold else d
                if t in oldnew and t != old:
                    deps.append(oldnew[t])
            it["depends_on"] = sorted(set(deps))
            reduced.append(it)
        changes = [{"index": i, "verdict": str(vby[i].get("verdict")),
                    "fold_into": vby[i].get("fold_into"),
                    "why": vby[i].get("why")} for i in sorted(remove)]
        return reduced, changes
    except Exception:
        return items, []


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
    """A sceptic trims sprawl toward the fewest PRs, its merges/drops are actually
    applied, then deterministic structural checks run on the trimmed set. Returns
    the possibly-reduced items so the plan itself shrinks — the reviewer has teeth,
    not just an opinion."""
    rs.set_phase(eid, "assess")

    # 1. Sceptic: which items should merge into another or drop entirely.
    verdicts: list[dict] = []
    model, token, _ = _pick_model(cfg)
    if model is not None:
        summary = "\n".join(
            f"{i}. [{it.get('repo')}] {it.get('title')} — files: "
            f"{', '.join(it.get('files') or []) or 'none named'} — "
            f"{it.get('rationale') or ''}"
            for i, it in enumerate(items))
        text, _meta = llm.complete(
            model, token, ASSESS_SYSTEM,
            f"PROPOSED SPLIT ({len(items)} PRs)\n{summary}", max_tokens=3000)
        obj = RunEngine._extract_json(text or "") or {}
        verdicts = obj.get("verdicts") or []
        if obj.get("overall"):
            rs.emit(eid, "assess", "info", "Reviewer's read",
                    {"text": str(obj["overall"])[:1500],
                     "recommended_pr_count": obj.get("recommended_pr_count")})

    # 2. Apply the merges/drops. Fail-safe: items come back unchanged on any doubt.
    before = len(items)
    items, changes = _apply_verdicts(items, verdicts)
    if changes:
        rs.emit(eid, "assess", "finding",
                f"Trimmed the split from {before} to {len(items)} PR(s)",
                {"folded_or_dropped": changes}, level="warn")

    # 3. Structural checks on the FINAL (trimmed) set. Two items editing the same
    # file cannot be reviewed or reverted independently — the parallel-PR conflict,
    # caught before it exists.
    problems: list[dict] = []
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
        rs.emit(eid, "assess", "warn",
                f"{len(problems)} structural problem(s) in the split",
                problems, level="warn")
    else:
        rs.emit(eid, "assess", "done",
                "No overlapping files, every item has a check, dependencies resolve")

    return {"items": items, "problems": problems, "verdicts": verdicts,
            "changes": changes}


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
        items = assessment["items"]        # the trimmed set the sceptic left standing

        # Council plan-review: before a human approves the split, a model panel judges
        # whether the PLAN actually serves the goal (missing requirements, overbuild,
        # wrong approach) — plan-to-pr's spec sign-off as a gate. Advisory to the human,
        # not an auto-block; surfaced in the approval card. Best-effort.
        council = None
        try:
            plan_text = "\n".join(
                f"{i+1}. [{it.get('repo','')}] {it.get('title','(untitled)')} — "
                f"{str(it.get('rationale') or it.get('description') or '')[:300]}"
                for i, it in enumerate(items))
            rs.emit(epic_id, "council", "step", "Council reviewing the plan")
            council = Council.plan_review(
                cfg, epic["goal"], plan_text,
                on_event=lambda ph, k, t, d=None: rs.emit(epic_id, "council", "info", t))
            rs.emit(epic_id, "council", "done",
                    f"Council: {council.get('verdict')}",
                    {"verdict": council.get("verdict"),
                     "must_fix": council.get("must_fix", []),
                     "should_fix": council.get("should_fix", []),
                     "summary": council.get("summary", ""),
                     "panel": council.get("panel", [])})
        except Exception as e:
            rs.emit(epic_id, "council", "warn",
                    f"Council review skipped: {type(e).__name__}", level="warn")

        rs.clear_items(epic_id)
        for i, it in enumerate(items):
            iid = rs.add_item(epic_id, i, it)
            probs = [p for p in assessment["problems"] if p.get("item") == i
                     or i in (p.get("items") or [])]
            rs.set_item(iid, assessment={"verdict": None, "problems": probs})

        rs.set_state(epic_id, {"repos": repos, "facts": facts[:20000],
                               "council": council})
        rs.set_phase(epic_id, "approve")
        rs.emit(epic_id, "approve", "info", "Waiting for you to approve the split",
                {"pull_requests": len(items),
                 "structural_problems": len(assessment["problems"]),
                 "council_verdict": (council or {}).get("verdict"),
                 "council_must_fix": (council or {}).get("must_fix", []),
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

def _sibling_context(rs: RunStore, items: list[dict], nxt: dict) -> str | None:
    """A compact view of the files this item's dependencies already wrote — enough
    for a dependent item to build on them (signatures + the first chunk of body),
    not the full files, which would bloat the build prompt until the model rambles
    instead of emitting the files JSON."""
    parts = []
    for d in nxt.get("depends_on", []):
        dep = next((i for i in items if i["seq"] == d), None)
        if not dep or not dep.get("run_id"):
            continue
        dc = rs.get(dep["run_id"])
        st = dc.get("state") if dc else None
        st = json.loads(st) if isinstance(st, str) else (st or {})
        ws = f"/work/{dep['run_id']}"
        for df in (st.get("reviewed") or {}).get("diffs", []):
            try:
                content = open(os.path.join(ws, df["path"]), encoding="utf-8").read()
            except Exception:
                continue
            # head: the imports + the public surface (signatures, dataclasses, the
            # first ~60 lines), so the dependent item knows the names to call
            head = "\n".join(content.splitlines()[:60])
            if len(content) > 4000:
                head += f"\n… ({len(content) - 4000} more chars; full file exists once merged) …"
            parts.append(f"--- {df['path']} (added by: {dep['title']}) ---\n{head[:4000]}")
    return "\n\n".join(parts) if parts else None


def advance(epic_id: str, index_db: str) -> None:
    """Start every item whose dependencies are BUILT (at their gate or already
    merged) — so the whole feature builds up front and waits for approval in a
    batch, not one-approve-at-a-time. A dependent item is handed its dependencies'
    just-built files to build on, and stacks its PR on their branch."""
    started = []
    rs = RunStore()
    try:
        items = rs.items(epic_id)

        def cstat(i):
            if i["status"] == "complete":
                return "complete"
            if i.get("run_id"):
                c = rs.get(i["run_id"])
                return c["status"] if c else None
            return None
        # a dependency counts as satisfied once its change is built (at its gate)
        built = {i["seq"] for i in items
                 if cstat(i) in ("awaiting_approval", "complete")}
        ready = [i for i in items if i["status"] == "pending"
                 and all(d in built for d in i["depends_on"])]
        if not ready:
            if any(i["status"] == "running" for i in items):
                return
            left = [i for i in items if i["status"] not in ("complete", "skipped")]
            if left:
                rs.finish(epic_id, "blocked",
                          reason=f"{len(left)} item(s) cannot start — their "
                                 f"dependencies did not build.",
                          next_action="Review the rejected or failed items below.")
            else:
                urls = [i for i in items if i["status"] == "complete"]
                rs.finish(epic_id, "complete",
                          reason=f"All {len(urls)} pull request(s) opened.",
                          next_action="Review them on GitHub.")
            return

        # Cap how many build at once: the whole feature still builds up front, but
        # paced so the shared Claude bridge is not overwhelmed (a hammered single
        # session hangs/fails). Each item reaching its gate re-triggers advance, so
        # the next ones start automatically.
        MAX_CONCURRENT = 2
        building_now = sum(1 for i in items if i["status"] == "running"
                           and cstat(i) in ("running", "queued"))
        ready = ready[:max(0, MAX_CONCURRENT - building_now)]
        if not ready:
            return

        epic_goal = rs.get(epic_id)["goal"]
        for nxt in ready:
            cur = next((i for i in rs.items(epic_id) if i["id"] == nxt["id"]), None)
            if cur is None or cur["status"] != "pending":   # a racing advance won it
                continue
            goal = (f"{nxt['title']}\n\nThis is one piece of a larger feature: "
                    f"{epic_goal}\n\nWhy this is its own change: "
                    f"{nxt['rationale']}\n\nIt must satisfy: "
                    + "; ".join(nxt["acceptance"]))
            child = rs.create(nxt["repo"], goal,
                              (nxt["files"] or [None])[0], None,
                              kind="run", parent_id=epic_id)
            sib = _sibling_context(rs, items, nxt)
            if sib:
                rs.set_state(child, {"sibling_ctx": sib})
            # Stack on a same-repo dependency's branch. It exists on the remote once
            # that dependency is approved; open_pr falls back to dev if it is not yet
            # there (e.g. you approve out of order).
            stack = [i for i in items if i["seq"] in nxt["depends_on"]
                     and i["repo"] == nxt["repo"] and i.get("run_id")]
            if stack:
                dep = max(stack, key=lambda i: i["seq"])
                base = branch_name(dep["title"], dep["run_id"])
                rs.set_base(child, base)
                rs.emit(epic_id, "execute", "info",
                        f"Item {nxt['seq'] + 1} stacks on {base}")
            rs.set_item(nxt["id"], status="running", run_id=child)
            rs.emit(epic_id, "execute", "step",
                    f"Item {nxt['seq'] + 1}/{len(items)}: {nxt['title']}",
                    {"repo": nxt["repo"], "run": child, "files": nxt["files"]})
            started.append(child)
        rs.db.execute("UPDATE runs SET status='running', phase='execute' WHERE id=?",
                      (epic_id,))
        rs.db.commit()
    finally:
        rs.close()
    for child in started:                       # launch outside the store lock
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
