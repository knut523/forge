# AGENTS.md — working on forge

Read this before changing anything. It is the repo's own instructions to whoever
is editing it, human or agent.

## What forge is

An independent engineering loop that contributes features to existing repos:
index the code → ground a plan in it → build on a branch → verify → **show a
human** → open a PR only once that human approves.

It is deliberately independent of the Prometheus platform: its own container,
its own SQLite files, its own auth. It borrows styling and nothing else. The one
network coupling is `docker network connect prometheus_default forge-ui`, which
exists solely so forge can reach the Claude bridge at `http://ide:8930`.

## The invariants — do not break these

1. **Nothing reaches GitHub without a human approving that specific run.** This
   is structural, not a rule to remember. `GITHUB_WRITE` is read in exactly two
   functions, each reachable only from an explicit human action, never by falling
   through a pipeline:
   - `runs/engine.py::ship_it` — open a PR, after a run's approval is recorded.
   - the `POST /api/prs/{owner}/{repo}/{number}/comment` handler in `api/app.py`
     — post a review comment, only when a human clicks Post on a draft they have
     seen and edited; it posts exactly that body and reads the token once, here.
   Do not read `GITHUB_WRITE` anywhere else. Drafting a review (`runs/pr_review.py`)
   is read-only and never touches the write token.

2. **Two tokens.** Read token for cloning, indexing, listing PRs, and drafting a
   review — everything before a gate. Write token for push, PR, and posting an
   approved review comment only. Both read and write support a per-org key
   (`github_read_token:<owner>` / `github_write_token:<owner>`) so prometheus work
   and olaf work stay behind separate credentials, with the global key as
   fallback. `verify` checks a token filed as read does not carry write scope, and
   says so plainly when GitHub won't tell us (fine-grained tokens).

3. **Precision over recall in the index.** An unresolved edge is visible in
   `resolved_pct` and can be reasoned about; a *wrong* edge silently lies to the
   engineer about what its change will break. The repo-wide unique-name fallback
   applies to bare calls only, never member calls. A name bound by an unresolved
   import is external. If you are tempted to guess an edge, leave it null.

4. **Terminal states must not flatter.** `complete` means a PR was opened.
   Running out of implemented phases is `incomplete`. A phase that could not do
   its job is `blocked`. Every terminal state carries `stopped_reason` and
   `next_action`. Never reintroduce a bare "done".

5. **A phase that cannot work says why and the run continues degraded.** With no
   engineer model, Plan reports the reason *and* prints the grounding it would
   have used. Crashing, or silently skipping, are both worse.

6. **The API opens the index read-only** via the sqlite connection (`mode=ro`),
   not via a read-only mount. A WAL-mode database cannot be opened on a
   read-only filesystem at all — SQLite must create `-shm` even to read.

## Layout

```
forge/indexer/   tree-sitter parse, sqlite store, graph resolution, wayfinder, PRs
forge/runs/      run engine, phases, review/approve/PR (ship.py), epics, memory
forge/config/    encrypted credentials, model registry, provider probes, llm client
forge/api/       FastAPI: JSON endpoints + serves the single-page UI
forge/ui/        one self-contained index.html — no build step, no node process
tools/           forge + forge-src, installed into the Prometheus IDE
```

Storage: `/data/forge.db` is the code index (disposable, rebuildable).
`/config/` holds credentials, runs and memory (durable, never rebuilt).
`/work/<run_id>` is where a build writes. **Never write to a source tree.**

## Dev loop from the Prometheus IDE

```bash
forge-src status     # who is mid-edit; host git log
forge-src pull       # /opt/forge -> /workspace/forge-src
#   ...edit /workspace/forge-src...
forge-src push       # refuses if the host repo is dirty — commit there first
forge-src build      # rebuild image, restart forge-ui, print health
```

Then commit on the host (`/opt/forge`), which is canonical and holds the history.

Gotchas that have cost real time:
- `docker restart forge-ui` reuses the **old image**. Recreate the container.
- Schema migrations run on the WRITE path only; the API opens read-only, so
  after a schema change you must re-index before the API sees the new column.
- Do **not** restart `prometheus-ide-1`: it holds long-lived tmux sessions and
  the Claude bridge, neither of which is persisted in the image.
- Reasoning models emit `<think>` blocks full of braces — strip before JSON
  extraction or a brace matcher parses the deliberation as the answer.

## Style

Match what is there. Comments explain *why*, especially where a choice looks
wrong until you know the incident behind it — those comments are load-bearing,
do not tidy them away. Prefer the smallest change that meets the ask. When you
find a real problem outside your task, say so rather than fixing it silently.
