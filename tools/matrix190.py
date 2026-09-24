"""Coder×reviewer 'ready' matrix — the honest answer to 'what is the actual strongest'.

For PR #190 (the one forge can genuinely finish), for each CODER model, run a fix loop:
  review (adversarial, minimax = the cheap workhorse reviewer) →
  coder rewrites the touched files to fix the confirmed findings →
  re-review → repeat until the reviewer confirms no high/medium findings, or max rounds.
Then a FINAL gate review with claude-session (the strong model) on the patched tree.

Measures per coder: rounds to converge, wall-time, final verdict, whether claude agrees.
Read-only against GitHub: operates entirely inside a scratch clone; never pushes.
"""
import json
import os
import shutil
import time
from pathlib import Path

from forge.config import llm
from forge.config.store import ConfigStore
from forge.indexer import github as gh, prs as PRs
from forge.indexer.store import Store
from forge.runs import review_adversarial as ADV, review_exec as REx
from forge.runs.engine import BUILD_SYSTEM, _extract_json, _recover_files
from forge.runs.pr_review import _local_repo, _tok

OWNER, REPO, NUM = "WirStrom1", "olaf-admin", 190
MAX_ROUNDS = 4
CODERS = ["minimax-engineer", "claude-session"]   # workhorse first
REVIEWER = "minimax-engineer"                       # cheap iteration reviewer
FINAL_GATE = "claude-session"                       # strong final gate


def _read(root, rel):
    p = Path(root) / rel
    return p.read_text("utf-8", "replace") if p.is_file() else None


def _fix(cfg, coder, root, findings, changed):
    """Ask the coder to rewrite the touched files to fix the findings. Full-file writes
    (the engine's robust contract), applied into the clone. Returns files written."""
    touched = []
    for f in findings:
        fp = f.get("file")
        if fp and fp not in touched:
            touched.append(fp)
    # also include any changed file a finding names loosely
    ctx = []
    for rel in touched[:8]:
        body = _read(root, rel)
        if body is not None:
            ctx.append(f"--- FILE {rel} ---\n{body[:60000]}")
    flist = "\n".join(f"- [{f.get('severity')}] {f.get('file')}: {f.get('detail')}"
                      for f in findings)
    user = (f"GOAL\nFix the review findings below in this PR without breaking anything else.\n\n"
            f"REVIEW FINDINGS TO FIX\n{flist}\n\n"
            f"CURRENT FILES\n{chr(10).join(ctx) or '(none resolved)'}")
    text, meta = llm.complete(coder, _tok(cfg, coder), BUILD_SYSTEM, user, max_tokens=24000)
    if not text:
        return [], meta.get("error", "coder call failed")
    obj = _extract_json(text) or {}
    files = obj.get("files") if isinstance(obj.get("files"), list) else _recover_files(text)
    written = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        rel, content = str(f.get("path", "")).strip(), f.get("content")
        if not rel or not isinstance(content, str):
            continue
        dest = (Path(root) / rel).resolve()
        if not str(dest).startswith(str(Path(root).resolve())):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, "utf-8")
        written.append(rel)
    return written, None


def _review(cfg, model, root, diff, changed, note=""):
    prepared = {"dir": root, "scratch": None}   # reuse the (patched) clone; caller owns it
    return ADV.review(cfg, model, _tok(cfg, model), REPO, diff, changed,
                      goal=f"PR #{NUM} {note}", prepared=prepared,
                      on_event=lambda ph, k, t, d=None: None)


def run_coder(cfg, store, coder_name, diff, changed, base_diff_note):
    coder = cfg.get_model(coder_name)
    reviewer = cfg.get_model(REVIEWER)
    prep = REx.prepare_pr(_local_repo(store, OWNER, REPO), f"{OWNER}/{REPO}", NUM,
                          gh.read_token(OWNER))
    if not prep.get("dir"):
        return {"coder": coder_name, "error": prep.get("error")}
    root = prep["dir"]
    t0 = time.time()
    rounds = []
    try:
        for r in range(MAX_ROUNDS):
            rev = _review(cfg, reviewer, root, diff, changed, note=f"round {r}")
            fnd = rev.get("findings", [])
            blocking = [f for f in fnd if str(f.get("severity")).lower() in ("high", "medium")]
            rounds.append({"round": r, "verdict": rev.get("verdict"),
                           "findings": len(fnd), "blocking": len(blocking)})
            print(f"  [{coder_name}] round {r}: {rev.get('verdict')} "
                  f"{len(fnd)} findings ({len(blocking)} blocking)", flush=True)
            if not blocking:
                break
            written, err = _fix(cfg, coder, root, blocking, changed)
            print(f"  [{coder_name}] fix: wrote {len(written)} file(s)"
                  + (f" — {err}" if err else ""), flush=True)
            if err or not written:
                rounds[-1]["fix_failed"] = err or "no files written"
                break
        # FINAL strong gate on the patched tree
        gate = _review(cfg, cfg.get_model(FINAL_GATE), root, diff, changed, note="FINAL gate")
        gblock = [f for f in gate.get("findings", [])
                  if str(f.get("severity")).lower() in ("high", "medium")]
        dt = time.time() - t0
        return {"coder": coder_name, "rounds": rounds, "seconds": round(dt),
                "converged": not rounds[-1].get("blocking") and not rounds[-1].get("fix_failed"),
                "final_gate_verdict": gate.get("verdict"),
                "final_gate_blocking": len(gblock),
                "ready": len(gblock) == 0}
    finally:
        if prep.get("scratch"):
            shutil.rmtree(prep["scratch"], ignore_errors=True)


def main():
    cfg = ConfigStore()
    store = Store(os.environ.get("FORGE_DB", "/data/forge.db"), readonly=True)
    tok = gh.read_token(OWNER)
    diff = PRs.diff(OWNER, REPO, NUM, tok)
    d = PRs.detail(OWNER, REPO, NUM, tok)
    changed = [f["path"] for f in d.get("files", [])]
    print(f"=== #{NUM}: {len(changed)} files | reviewer={REVIEWER} | final={FINAL_GATE} ===",
          flush=True)
    results = []
    for c in CODERS:
        print(f"\n--- CODER: {c} ---", flush=True)
        results.append(run_coder(cfg, store, c, diff, changed, ""))
    print("\n=== MATRIX RESULT ===")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    store.close(); cfg.close()


if __name__ == "__main__":
    main()
