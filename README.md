# forge

An independent engineering loop that contributes features to existing repos:
index the code → ground a plan in it → build on a branch → verify (including
visually) → **show a human** → open a PR only once that human approves.

Independent by design. It shares no database, no auth and no container with the
Prometheus platform; it borrows only ideas (and, later, styling). The one thing
it must never do is act on a repo without approval — see *Two tokens* below.

Status: **Phase 1 — the code index.** Nothing here talks to GitHub yet.

## Why an index first

An engineer loop that cannot see the codebase invents. It re-implements what
already exists, misses the callers it is about to break, and grounds its plan in
whatever the prompt happened to mention. Every later stage — the spec, the
duplication check, the blast radius in the human review — reads from this index.

## The index

`tree-sitter` parses Python, TypeScript, TSX and JavaScript into three tables:

| table     | what it holds                                   |
|-----------|-------------------------------------------------|
| `symbols` | definitions: classes, functions, methods, interfaces, types, enums, consts |
| `refs`    | call and JSX-usage edges, resolved to symbol ids |
| `imports` | file-level dependency edges                      |

Plus an FTS5 index over names, signatures and docstrings. The store is a single
SQLite file, so an index can be copied, diffed, or thrown away without a server.

### Precision over recall — on purpose

An unresolved edge is visible in `resolved_pct` and can be reasoned about. A
**wrong** edge silently lies to the engineer about what its change will break.
So resolution goes only as far as the evidence does:

1. a symbol of that name in the same file
2. a symbol reached through a resolved import in that file
3. a repo-wide unique name — **bare calls only**

Rule 3 never applies to a member call: for `datetime.now()` the receiver carries
the meaning, and matching on `now` alone once pointed 302 call sites at an
unrelated local helper. Likewise, a name bound by an import we could not resolve
is treated as external, so `pydantic`'s `Field` is not confused with a `Field`
class that happens to live in the repo.

Measured on this platform's own codebase (493 files, 5,173 symbols, 39,075 refs):
65% of refs are genuinely external (stdlib, npm), and **44.7%** of the resolvable
remainder is linked. The unlinked remainder is almost entirely member calls whose
receiver type we do not infer yet.

### What it skips, and why

`node_modules`, `dist`, `.next`, `__pycache__` and friends, plus directories
matching artefact patterns like `-dist`, `.bak`, `.old` — a bundled copy
otherwise dominates the symbol table and steals edges from the real definition.
Files whose *shape* is generated (mean line length > 200) are skipped too.

The crawl deliberately does **not** honour `.gitignore` by default. Two reasons
learned from real trees: anything the engineer just wrote is untracked, and
`.gitignore` hides real source here, not only build output. Pass `--git-only`
where the ignore rules genuinely are the boundary.

## Use

```bash
docker build -t forge:dev .

# index a working tree (read-only mount is enough)
docker run --rm -v /opt/forge/data:/data -v /path/to/repo:/repo:ro \
  forge:dev python -m forge.cli index /repo --name myrepo

# then interrogate it
R="docker run --rm -v /opt/forge/data:/data forge:dev python -m forge.cli"
$R overview                  # languages, symbol kinds, biggest files
$R search "tariff price"     # full-text over names, signatures, docs
$R sym calculateTariff       # where is it defined
$R callers calculateTariff   # who calls it, from which function
$R callees calculateTariff   # what it calls
$R blast calculateTariff --depth 3   # transitive callers = change risk
$R file src/lib/tariff.ts    # symbols, imports, imported-by
$R hotspots                  # load-bearing symbols
$R orphans                   # exported but unreferenced (dead-code candidates)
```

Add `--json` for machine-readable output — that is how the engineer loop will
consume it.

## Two tokens

A fixed convention, enforced rather than remembered:

- a **read** token — clone and index. Everything up to the human gate uses only this.
- a **write** token — push a branch and open a PR. Unlocked *only* after a human
  approves that specific run, and used for nothing else.

## Roadmap

1. **Code index** ← done
2. Capture / spec / plan, with the `plan-to-pr` gates (freshness, anti-duplication,
   parallel-PR) as code rather than prose
3. Build on a branch, grounded in the index
4. Visual verification — Playwright screenshots of every changed state
5. Human review UI — diff, screenshots, blast radius, approve / request changes
6. PR on approval: base `dev`, ready for review; docs/plan-only changes treated
   as low-risk

## Next on the index

Receiver type inference, starting with the cheap and precise case: `self.x()` and
`this.x()` resolve within the enclosing class. That is where most of the
currently-unresolved in-repo member calls live.
