# forge

An independent engineering loop that contributes features to existing repos:
index the code → ground a plan in it → build on a branch → verify (including
visually) → **show a human** → open a PR only once that human approves.

Independent by design. It shares no database, no auth and no container with the
Prometheus platform; it borrows only ideas (and, later, styling). The one thing
it must never do is act on a repo without approval — see *Two tokens* below.

Status: **Phase 1 — the code index, the wayfinder, and a UI.** The only GitHub
access so far is read: listing an org and cloning. Nothing is ever pushed.

UI: `/forge/` behind HTTP Basic, served as a static page by the API — no second
Node process, which matters on a host that already OOMs.

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

## Wayfinder — what else does this change touch?

Runs *before* a plan is written. The costliest mistake a repo-contributing
engineer makes is reasoning inside one repo when the change spans several.

Separate repos share no imports and no symbol ids, so the symbol graph cannot
see the seam. Wayfinder works on evidence instead, and labels it:

- **route** — a URL path in one repo extends a route declared in another. A
  backend declares `/start` under a router mounted at `/api/v1/ace`; the
  frontend writes the whole path. Matching is by suffix, and the leftover
  prefix must itself appear in the declaring repo before a match is called
  `high` — corroboration, not coincidence.
- **shared-name** — the same name is exported, or called-but-unresolved,
  elsewhere. A duplication and shared-contract hint, reported at low confidence.

Everything carries file, line and a confidence. Nothing is asserted as fact: a
wayfinder that guessed confidently would be worse than none, because it would
be believed.

Two filters keep it honest, both added after real false positives:

- absolute filesystem paths (`/dev/null`, `/tmp/file`) are never treated as
  routes — they match the shape perfectly and link repos that share nothing;
- a one-segment path (`/test`, `/stream`) never couples two repos on its own.
  It only counts when a multi-segment mount point corroborates it.

Measured across four indexed repos — this platform's backend and frontend plus
two unrelated open-source repos — wayfinder reports coupling **only** between
the backend and frontend (177 route matches, 174 high), and none at all for the
unrelated pair.

```bash
$R links                                        # who is coupled to whom
$R --repo prom-backend impacts app/api/x.py --file
$R --repo prom-backend impacts calculateTariff
```

## Many repos, and whole orgs

The store is multi-repo from the ground up; every query takes `--repo`, and
wayfinder searches across all of them.

```bash
$R org my-org                                   # list (read-only)
$R index-org my-org --select api,web,shared     # a chosen few
$R index-org my-org --all --limit 40            # or the whole org
```

Clones are shallow and single-branch, and the checkout is deleted after
indexing unless `--keep` is passed. The read token comes from
`$FORGE_READ_TOKEN`, is never logged, and is scrubbed out of the clone's remote
URL so an abandoned checkout carries no credential. Without a token it still
works against public repos at GitHub's 60/hour anonymous limit. `--limit` is a
deliberate speed bump: cloning an entire org is a lot of disk and time.

## Two tokens

A fixed convention, enforced rather than remembered:

- a **read** token — clone and index. Everything up to the human gate uses only this.
- a **write** token — push a branch and open a PR. Unlocked *only* after a human
  approves that specific run, and used for nothing else.

They are separate named slots (`github_read_token`, `github_write_token`), not a
free-form string a caller can misspell into the wrong one.

**Verify checks the split.** `POST /api/settings/{key}/verify` asks GitHub who
the token is and reads back its scopes. A token filed as *read* that carries
`repo`, `workflow` or any other write scope is flagged immediately — a safety
rule nobody checks is a hope, not a rule. Fine-grained tokens don't expose
permissions this way; forge says so plainly rather than implying it passed.

## Credentials and models

Set through the UI's **Settings** tab, or the API. Values go in and never come
back out: every response carries the provider, the last four characters and when
it was set, never the secret.

Storage is a **separate SQLite file** from the index, for two reasons: the index
is disposable and gets rebuilt, and it is mounted read-only into the API
container — a property worth keeping. Only the config store is writable.

Secrets are AES-GCM encrypted at rest, with the key in `$FORGE_KEK` or a `0600`
file beside the database. Be clear-eyed about what that buys: it protects a
leaked database file, a backup or a careless `cat` — not someone who already has
root on the host.

Models are registered against a credential and, optionally, a **role**
(`engineer`, `council`, `judge`, `fast`) so a run can ask for a purpose rather
than a name. `List models` asks the provider what it actually offers, so an id
is picked rather than typed, and `Test` proves the credential works *and* that
the specific model id exists in that provider's list — reachability alone is not
the same answer.

```
PUT    /api/settings              {key, value, provider}
POST   /api/settings/{key}/verify
GET    /api/models · POST /api/models · POST /api/models/{name}/test
GET    /api/providers/{provider}/models
```

## Working on forge from the Prometheus IDE

The IDE container cannot see `/opt/forge`, and it should not be restarted to add
a bind mount — it holds long-lived tmux sessions. It does have the docker
socket, and the daemon resolves paths on the *host*, which is enough. Two
commands are installed at `/usr/local/bin` there (sources in `tools/`):

```bash
forge repos | overview | search | impacts | links | index      # query the index
forge-src status     # host git log + anything uncommitted, and the local copy
forge-src pull       # /opt/forge           -> /workspace/forge-src
forge-src push       # /workspace/forge-src -> /opt/forge
forge-src build      # rebuild the image and restart the UI container
```

`/opt/forge` is canonical: it holds the git history and the service is built
from it. `/workspace/forge-src` is a working copy.

**`push` refuses while the host repo is dirty.** That is the whole safety
property: uncommitted changes on the host mean someone is mid-edit, and a
wholesale copy would destroy their work without saying so. Commit first.

Note `/workspace/forge` is an unrelated 2026-06 project. The working copy is
deliberately `/workspace/forge-src`.

## Roadmap

1. **Code index** ← done
2. **Wayfinder + multi-repo/org indexing + UI** ← done
3. Capture / spec / plan, with the `plan-to-pr` gates (freshness, anti-duplication,
   parallel-PR) as code rather than prose — wayfinder runs first, so the plan
   knows about every repo it touches
3. Build on a branch, grounded in the index
4. Visual verification — Playwright screenshots of every changed state
5. Human review UI — diff, screenshots, blast radius, approve / request changes
6. PR on approval: base `dev`, ready for review; docs/plan-only changes treated
   as low-risk

## Next on the index

Receiver type inference, starting with the cheap and precise case: `self.x()` and
`this.x()` resolve within the enclosing class. That is where most of the
currently-unresolved in-repo member calls live.
