"""Run forge's full review on an external PR and print findings compactly.
usage: review_pr.py <owner> <repo> <number> [mode]"""
import os, sys, json
from forge.config.store import ConfigStore, GITHUB_READ, org_read_key
from forge.indexer.store import Store
from forge.runs import pr_review as PR

owner, repo, number = sys.argv[1], sys.argv[2], int(sys.argv[3])
mode = sys.argv[4] if len(sys.argv) > 4 else "full"
cfg = ConfigStore()
store = Store(os.environ.get("FORGE_DB", "/data/forge.db"), readonly=True)
try:
    from forge.runs.pr_review import _local_repo
    local = _local_repo(store, owner, repo)
    tok = cfg.get_secret(org_read_key(owner)) or cfg.get_secret(GITHUB_READ)
    out = PR.review(cfg, owner, repo, number, tok, store, local, mode=mode)
    if out.get("error") or out.get("why"):
        print("PROBLEM:", out.get("error") or out.get("why")); sys.exit(0)
    print(f"### {owner}/{repo}#{number} [{mode}] verdict={out.get('verdict')} "
          f"findings={len(out.get('findings',[]))} seeds={out.get('seeds')} "
          f"acc={out.get('acceptance',{}).get('source')}")
    for f in out.get("findings", []):
        print(f"- [{f.get('severity')}/{f.get('angle','')}] {f.get('file')}:{f.get('line','')} "
              f"-> {str(f.get('detail'))[:240].replace(chr(10),' ')}")
finally:
    store.close(); cfg.close()
