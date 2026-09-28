import os, json, traceback
from forge.config.store import ConfigStore
from forge.indexer.store import Store
from forge.runs import pr_review as PR

DB = os.environ.get("FORGE_DB", "/data/forge.db")
cfg = ConfigStore()
store = Store(DB, readonly=True)
try:
    tok = None
    from forge.config.store import GITHUB_READ, org_read_key
    tok = cfg.get_secret(org_read_key("WirStrom1")) or cfg.get_secret(GITHUB_READ)
    models = [m["name"] for m in cfg.list_models() if m["enabled"]]
    print("enabled models:", models)
    out = PR.review(cfg, "WirStrom1", "olaf-admin", 190, tok, store, "olaf-admin",
                    mode="light")
    print("verdict:", out.get("verdict"))
    print("seeds:", out.get("seeds"), "| acceptance:", out.get("acceptance", {}).get("source"))
    print("findings:", len(out.get("findings", [])))
    for f in out.get("findings", [])[:8]:
        print(f"  [{f.get('severity')}] {f.get('file')}: {str(f.get('detail'))[:130]}")
    if out.get("error") or out.get("why"):
        print("PROBLEM:", out.get("error") or out.get("why"))
except Exception:
    traceback.print_exc()
finally:
    store.close(); cfg.close()
