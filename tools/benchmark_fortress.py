"""Benchmark forge's review against a top human reviewer (Christoph / bizarrochris).

Ground truth = Christoph's actual review comments on each PR, pulled from the local
pr_history index. For each PR we run forge's full review, then an LLM judge counts
how many of Christoph's DISTINCT blocking findings forge independently surfaced
(recall) and whether forge's own findings are real (precision proxy).

Honest caveat: a PR may have moved since Christoph reviewed it (some blockers were
author-fixed), so a 'miss' can mean the bug is gone, not that forge failed. The judge
is told to ignore a Christoph finding that the current diff no longer contains.
"""
import json
import os
import sys

from forge.config.store import ConfigStore, GITHUB_READ, org_read_key
from forge.indexer.store import Store
from forge.runs import pr_review as PR, pr_history as H

import os as _os
_e0 = _os.environ.get("FORGE_E0")
PRS = ([tuple([x.split(":")[0], int(x.split(":")[1])] ) for x in _e0.split(",")]
       if _e0 else [("olaf-admin",190),("olaf-admin",181),("olaf-admin",175)])
GT_AUTHOR = "bizarrochris"

JUDGE = (
    "You are scoring an automated code reviewer (forge) against a top human reviewer "
    "(Christoph) on one PR. You get Christoph's review comments (ground truth) and "
    "forge's findings. Do this:\n"
    "1. From Christoph's comments, extract his DISTINCT blocking/substantive findings "
    "(ignore praise, style nits, and his own self-corrections). List them briefly.\n"
    "2. For EACH, decide if forge surfaced the same issue (match on the actual defect, "
    "not wording). If forge could not have seen it because the current diff no longer "
    "contains that code, mark it 'stale' and exclude it from scoring.\n"
    "3. Also count forge findings that are real defects NOT in Christoph's list (bonus).\n"
    "Respond ONLY JSON: {\"christoph_findings\": [{\"finding\":\"...\",\"forge_caught\":true,"
    "\"stale\":false}], \"forge_extra_real\": 0, \"note\":\"one line\"}"
)


def christoph_gt(owner, repo, number):
    c = H._db()
    rows = c.execute(
        "SELECT kind,path,line,body FROM comments WHERE repo LIKE ? AND number=? AND author=?"
        " ORDER BY created", (f"%/{repo}", number, GT_AUTHOR)).fetchall()
    c.close()
    parts = []
    for r in rows:
        loc = (r["path"] or "") + (f":{r['line']}" if r["line"] else "")
        parts.append((f"[{loc}] " if loc else "") + (r["body"] or "").strip())
    return "\n\n".join(parts)[:14000]


def main():
    cfg = ConfigStore()
    store = Store(os.environ.get("FORGE_DB", "/data/forge.db"), readonly=True)
    tok = cfg.get_secret(org_read_key("WirStrom1")) or cfg.get_secret(GITHUB_READ)
    models = [m for m in cfg.list_models() if m["enabled"]]
    judge_model = models[0]
    from forge.config import llm
    from forge.runs.pr_review import _local_repo, _tok
    tot_gt = tot_caught = tot_extra = 0
    print(f"{'PR':16} {'forge verdict':20} {'findings':9} recall(non-stale)")
    for repo, num in PRS:
      try:
        gt = christoph_gt("WirStrom1", repo, num)
        if not gt:
            print(f"{repo}#{num}: no Christoph comments in index — skip", flush=True); continue
        local = _local_repo(store, "WirStrom1", repo)
        try:
            out = PR.review(cfg, "WirStrom1", repo, num, tok, store, local, mode="adversarial")
        except Exception as e:
            print(f"{repo}#{num}: review error {e}", flush=True); continue
        if out.get("error") or out.get("why"):
            print(f"{repo}#{num}: {out.get('error') or out.get('why')}", flush=True); continue
        ff = out.get("findings", [])
        forge_txt = "\n".join(f"- [{f.get('severity')}/{f.get('angle','')}] "
                              f"{f.get('file')}: {str(f.get('detail'))[:220]}" for f in ff)
        user = (f"PR {repo}#{num}\n\nCHRISTOPH'S COMMENTS (ground truth):\n{gt}\n\n"
                f"FORGE'S FINDINGS:\n{forge_txt or '(none)'}")
        jtext, _ = llm.complete(judge_model, _tok(cfg, judge_model), JUDGE, user, max_tokens=2000)
        from forge.runs.engine import _extract_json
        j = _extract_json(jtext or "") or {}
        cf = [x for x in j.get("christoph_findings", []) if not x.get("stale")]
        caught = sum(1 for x in cf if x.get("forge_caught"))
        extra = j.get("forge_extra_real", 0) or 0
        tot_gt += len(cf); tot_caught += caught; tot_extra += extra
        rec = f"{caught}/{len(cf)}" if cf else "n/a"
        print(f"{repo}#{num:<10} {str(out.get('verdict'))[:20]:20} {len(ff):<9} {rec}  (+{extra} extra) {j.get('note','')[:60]}", flush=True)
      except Exception as e:
        print(f"{repo}#{num}: benchmark error {type(e).__name__}: {e}", flush=True)
    print("\n=== OVERALL ===")
    print(f"Christoph findings (non-stale): {tot_gt}")
    print(f"forge caught: {tot_caught}  -> recall {100*tot_caught/tot_gt:.0f}%" if tot_gt else "n/a")
    print(f"forge extra real findings (beyond Christoph): {tot_extra}")
    store.close(); cfg.close()


if __name__ == "__main__":
    main()
