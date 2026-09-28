"""Clone olaf-calc-api and fetch the #54/#55 PR branches into /tmp so their
current code can be reviewed and fixed. Token stays in-process (never printed)."""
import subprocess, sys
import forge.indexer.github as gh

tok = gh.read_token("WirStrom1")
if not tok:
    print("NO TOKEN"); sys.exit(1)

REPO = "WirStrom1/olaf-calc-api"
DEST = "/tmp/ocai"
url = f"https://x-access-token:{tok}@github.com/{REPO}.git"

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, **kw)
    return p.returncode, p.stdout, p.stderr

subprocess.run(["rm", "-rf", DEST])
rc, o, e = run(["git", "clone", "--no-single-branch", url, DEST])
print("clone rc", rc, (e[:200] if rc else ""))
# fetch PR refs while origin still carries the token
for n in (54, 55):
    rc, o, e = run(["git", "-C", DEST, "fetch", "origin", f"pull/{n}/head:pr{n}"])
    print(f"fetch pr{n} rc", rc, (e[:160] if rc else "ok"))
# scrub token from remote only after fetching
run(["git", "-C", DEST, "remote", "set-url", "origin", f"https://github.com/{REPO}.git"])
# show branch info + the house-number files on pr54
rc, o, e = run(["git", "-C", DEST, "log", "--oneline", "-3", "pr54"])
print("pr54 head:\n", o)
rc, o, e = run(["git", "-C", DEST, "diff", "--name-only", "origin/HEAD...pr54"])
print("pr54 changed files:\n", o)
rc, o, e = run(["git", "-C", DEST, "diff", "--name-only", "origin/HEAD...pr55"])
print("pr55 changed files:\n", o)
