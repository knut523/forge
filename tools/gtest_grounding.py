import traceback
import forge.indexer.github as gh
import forge.indexer.prs as PRs
import forge.runs.review_grounding as RG

tok = gh.read_token("WirStrom1")
print("token:", bool(tok))


def run(owner, repo, number, name):
    try:
        d = PRs.detail(owner, repo, number, tok)
        diff = PRs.diff(owner, repo, number, tok)
        changed = [f["path"] for f in d.get("files", [])]
        g = RG.build(name, d.get("body", ""), diff, changed)
        print(f"\n=== {repo}#{number} ({name}) ===")
        print("changed files:", len(changed), "| clone:", RG.clone_dir(name))
        print("acceptance:", g["acceptance"]["source"], "->",
              len(g["acceptance"]["criteria"]), "criteria; plan:", g["acceptance"]["plan_ref"])
        for c in g["acceptance"]["criteria"][:6]:
            print("   -", c[:110])
        print("seeds:", len(g["seeds"]))
        for s in g["seeds"]:
            print(f"   [{s['angle']}/{s['severity']}] {s['file']}: {s['detail'][:150]}")
        print("has_conventions:", g["has_conventions"])
    except Exception:
        traceback.print_exc()


run("WirStrom1", "olaf-admin", 190, "olaf-admin")
run("WirStrom1", "olaf-calc-api", 55, "olaf-calc-api")
