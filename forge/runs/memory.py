"""Repo memory — so the loop stops relearning the same things.

An engineer that reads the code perfectly and remembers nothing will make the
same wrong call every run: re-add a field that already exists, branch off the
wrong base, touch a generated file, get rejected for the same reason twice.
The index gives it sight; this gives it a past.

Three sources, deliberately distinct:

* **repo** — conventions read out of the repository itself (AGENTS.md, CLAUDE.md,
  CONTRIBUTING). Refreshed on every run, because the file is the truth.
* **run**  — lessons captured automatically when something goes wrong: a
  rejection, a verify failure. The reason a human gave is the most valuable
  sentence in the whole system and it used to be thrown away.
* **human** — anything you type in.

Memories are injected into the Plan and Build prompts. A memory that is never
injected is decoration, so `hits` is counted and shown.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY,
    repo       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    source     TEXT,
    run_id     TEXT,
    created_at TEXT NOT NULL,
    active     INTEGER DEFAULT 1,
    hits       INTEGER DEFAULT 0,
    -- A JSON array of pin strings (file:… / symbol:… / route:…). Pins are matched against the
    -- files a run touches at query time; they are NEVER a foreign key, so an unresolvable pin is
    -- inert rather than wrong — the same invariant the index holds for an edge it cannot resolve.
    pins            TEXT,
    -- The temporal axis is the git SHA, not wall-clock: a repo convention becomes false when the
    -- code changes, not when time passes. valid_from_sha is the commit at which we recorded this;
    -- valid_until_sha is stamped when a newer row replaces it, and NULL means we still believe it.
    valid_from_sha  TEXT,
    valid_until_sha TEXT,
    superseded_by   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_mem_repo ON memories(repo, active);

-- Full-text index over title+body, kept in lockstep by triggers. It is how the text channel finds
-- a lesson by what it says when no pin attaches it to the files a run touches. title/body never
-- change after insert (a re-learn writes a new row), so an update trigger is unnecessary.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(title, body);
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.id;
END;
"""

# Created only after _migrate() has ensured valid_until_sha exists (an old table won't have it, so
# this index can't live in the table DDL). At most one *live* memory per (repo, kind, title):
# superseded rows (valid_until_sha set) are exempt, so the history of a lesson — "we used Jest, we
# now use Vitest, because ESM" — coexists with its current form. This replaces the old table-level
# UNIQUE, whose ON CONFLICT upsert overwrote the previous body and destroyed exactly that transition.
LIVE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_mem_live ON memories(repo, kind, title)
    WHERE valid_until_sha IS NULL;
"""

KINDS = ("convention", "lesson", "gotcha", "decision")

# Pins attach a memory to a place in the code. They are plain strings compared at query time, so
# file pins (which survive refactors) are preferred over symbol pins (which churn); only pin when
# the attachment is unambiguous. An unrecognised prefix is dropped rather than stored wrong.
PIN_PREFIXES = ("file:", "symbol:", "route:")


def _normalize_pins(pins) -> str | None:
    """A clean JSON array of recognised pins, or None. Order-preserving, de-duplicated; anything
    without a known prefix is silently dropped so a malformed pin can never resolve to garbage."""
    if not pins:
        return None
    out: list[str] = []
    for p in pins:
        p = str(p).strip()
        if p.startswith(PIN_PREFIXES) and p not in out:
            out.append(p[:300])
    return json.dumps(out) if out else None


def _pins_of(row) -> list[str]:
    try:
        return json.loads(row["pins"] or "[]")
    except (TypeError, ValueError):
        return []


# Gotchas and lessons are the ones that stop a repeat mistake, so they lead when nothing else ranks.
_KIND_ORDER = {"gotcha": 0, "lesson": 1, "convention": 2, "decision": 3}

# Reciprocal-rank-fusion constant. 60 is the value the original RRF paper uses and everyone copies;
# it just damps how much the very top ranks dominate.
_RRF_K = 60

# Roughly one token per four characters — enough to size a budget without a tokenizer dependency.
def _tok(s: str) -> int:
    return max(1, len(s) // 4)


def _fts_query(touched) -> str:
    """An FTS5 MATCH query from the files/symbols a run touches: the last path or dotted segment of
    each, minus extension, quoted so a dotted or slashed value can't be read as FTS syntax. Empty
    when nothing usable — the caller must then skip the text channel rather than run a blank MATCH."""
    terms: list[str] = []
    for t in touched:
        seg = str(t).replace(":", "/").rstrip("/").split("/")[-1].split(".")[0].strip()
        if len(seg) >= 3 and seg.lower() not in ("src", "lib", "app", "index"):
            q = '"' + seg.replace('"', '') + '"'
            if q not in terms:
                terms.append(q)
    return " OR ".join(terms)

# Files a repo uses to tell contributors how it wants to be worked on.
CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md",
                    "docs/AGENTS.md", ".github/CONTRIBUTING.md")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get(
            "FORGE_MEMORY_DB", "/config/forge-memory.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.executescript(LIVE_INDEX)

    def _migrate(self) -> None:
        """Additive and idempotent. A DB created before the SHA-supersession rework has the old
        UNIQUE(repo, kind, title) baked into the table, and SQLite cannot drop a constraint in
        place — so on those we rebuild the table once into the new shape, preserving every row.
        A newer DB only needs any missing columns filled in (the store.py migration pattern)."""
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
        if row and "UNIQUE (repo, kind, title)" in (row["sql"] or ""):
            # The renamed table keeps ix_mem_repo and the fts triggers by name; free them so SCHEMA
            # recreates them against the new table. The final INSERT fires mem_ai and refills fts.
            self.db.executescript(
                "ALTER TABLE memories RENAME TO memories_old;"
                "DROP INDEX IF EXISTS ix_mem_repo;"
                "DROP TRIGGER IF EXISTS mem_ai;"
                "DROP TRIGGER IF EXISTS mem_ad;"
                "DROP TABLE IF EXISTS memories_fts;"
                + SCHEMA +
                "INSERT INTO memories"
                " (id, repo, kind, title, body, source, run_id, created_at, active, hits)"
                " SELECT id, repo, kind, title, body, source, run_id, created_at, active, hits"
                " FROM memories_old;"
                "DROP TABLE memories_old;")
            self.db.commit()
            return
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(memories)")}
        for col, decl in (("pins", "TEXT"), ("valid_from_sha", "TEXT"),
                          ("valid_until_sha", "TEXT"), ("superseded_by", "INTEGER")):
            if col not in have:
                self.db.execute(f"ALTER TABLE memories ADD COLUMN {col} {decl}")
        # A DB that already had the SHA columns but predates the fts index has an empty fts; the
        # triggers only fire on new writes, so backfill the existing rows once.
        fts_n = self.db.execute("SELECT count(*) c FROM memories_fts").fetchone()["c"]
        mem_n = self.db.execute("SELECT count(*) c FROM memories").fetchone()["c"]
        if fts_n == 0 and mem_n > 0:
            self.db.execute(
                "INSERT INTO memories_fts(rowid, title, body) SELECT id, title, body FROM memories")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def add(self, repo: str, kind: str, title: str, body: str,
            source: str = "human", run_id: str | None = None,
            pins=None, sha: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        title, body = title.strip()[:200], body.strip()[:4000]
        if not title or not body:
            raise ValueError("title and body are required")
        pins_json = _normalize_pins(pins)

        # Relearning a lesson must NOT overwrite the old one. The overwrite loses the transition
        # ("we used Jest, we now use Vitest, because ESM"), which is more useful than either end,
        # and it silently clobbers an unrelated lesson that happened to share a title. So we stamp
        # the current belief with valid_until_sha and insert the new one beside it, linked by
        # superseded_by — "what did we believe at commit X" stays answerable. An identical re-add
        # is a no-op beyond refreshing provenance: no churn, no second row.
        live = self.db.execute(
            "SELECT * FROM memories WHERE repo=? AND kind=? AND title=?"
            " AND valid_until_sha IS NULL", (repo, kind, title)).fetchone()
        if live is not None and live["body"] == body and (live["pins"] or None) == pins_json:
            self.db.execute("UPDATE memories SET source=?, run_id=? WHERE id=?",
                            (source, run_id, live["id"]))
            self.db.commit()
            return self.get(repo, kind, title)

        # Retire the old row FIRST — the live index (ux_mem_live) permits only one row per title
        # with valid_until_sha NULL, so the replacement cannot be inserted while the old one is
        # still live. valid_until_sha must be non-null to leave that index and the default recall
        # filter; when the caller has no SHA, 'unknown' is inert rather than wrong, the same rule a
        # pin follows (the run engine does pass the real head SHA). superseded_by is linked once we
        # have the new id.
        if live is not None:
            self.db.execute("UPDATE memories SET valid_until_sha=?, active=0 WHERE id=?",
                            (sha or "unknown", live["id"]))
        cur = self.db.execute(
            "INSERT INTO memories"
            " (repo, kind, title, body, source, run_id, created_at, pins, valid_from_sha)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (repo, kind, title, body, source, run_id, _now(), pins_json, sha))
        new_id = cur.lastrowid
        if live is not None:
            self.db.execute("UPDATE memories SET superseded_by=? WHERE id=?",
                            (new_id, live["id"]))
        self.db.commit()
        return self.get(repo, kind, title)

    def get(self, repo: str, kind: str, title: str) -> dict | None:
        """The live memory for this title — several rows may share it once one supersedes another."""
        r = self.db.execute(
            "SELECT * FROM memories WHERE repo=? AND kind=? AND title=?"
            " AND valid_until_sha IS NULL", (repo, kind, title)).fetchone()
        return dict(r) if r else None

    def list(self, repo: str | None = None, include_inactive: bool = False) -> list[dict]:
        q = "SELECT * FROM memories WHERE 1=1"
        a: list = []
        if repo:
            q += " AND repo=?"
            a.append(repo)
        if not include_inactive:
            q += " AND active=1"
        q += " ORDER BY repo, kind, id DESC"
        return [dict(r) for r in self.db.execute(q, a)]

    def delete(self, mem_id: int) -> bool:
        cur = self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self.db.commit()
        return cur.rowcount > 0

    def _fts_ids(self, repo: str, query: str) -> list[int]:
        """Live-memory ids matching an FTS query for this repo, best first. Empty on a blank or
        malformed query — an unusable MATCH must be inert, not raise."""
        if not query:
            return []
        try:
            rows = self.db.execute(
                "SELECT f.rowid AS id FROM memories_fts f"
                " JOIN memories m ON m.id = f.rowid"
                " WHERE m.repo=? AND m.valid_until_sha IS NULL AND memories_fts MATCH ?"
                " ORDER BY rank LIMIT 100", (repo, query)).fetchall()
            return [r["id"] for r in rows]
        except sqlite3.OperationalError:
            return []

    def recall(self, repo: str, touched=None, cap_tokens: int = 1800) -> list[dict]:
        """The memories worth putting in front of a run, ranked, token-capped, no side effects.

        Given `touched` (the files/symbols the run is about), two channels are fused by reciprocal
        rank: a PIN channel (memories attached to a touched place, weighted ~2x) and a TEXT channel
        (FTS over title+body). Coding gotchas are overwhelmingly path-local, so a memory that
        neither pins to nor mentions a touched file is dropped — injecting everything is what blows
        the token budget. With no `touched` (Plan time, before files are known) it is the full set,
        ranked by kind then recency. The cap is on TOKENS, not rows: 25 verbose lessons are ten
        times the budget of 25 terse ones."""
        live = {r["id"]: dict(r) for r in self.db.execute(
            "SELECT id, kind, title, body, pins, valid_from_sha FROM memories"
            " WHERE repo=? AND valid_until_sha IS NULL", (repo,))}
        if not live:
            return []
        touched = {str(t).strip() for t in (touched or []) if str(t).strip()}

        if touched:
            def hit(r):
                return any(p.split(":", 1)[-1] in touched for p in _pins_of(r))
            pinned = sorted((r for r in live.values() if hit(r)), key=lambda r: -r["id"])
            texted = self._fts_ids(repo, _fts_query(touched))
            scores: dict[int, float] = {}
            for i, r in enumerate(pinned):                      # pin channel, ~2x weight
                scores[r["id"]] = scores.get(r["id"], 0.0) + 2.0 / (_RRF_K + i + 1)
            for i, mid in enumerate(texted):                    # text channel
                if mid in live:
                    scores[mid] = scores.get(mid, 0.0) + 1.0 / (_RRF_K + i + 1)
            ordered = [live[mid] for mid, _ in
                       sorted(scores.items(), key=lambda x: (-x[1], -x[0]))]
        else:
            ordered = sorted(live.values(),
                             key=lambda r: (_KIND_ORDER.get(r["kind"], 9), -r["id"]))

        out, used = [], 0
        for r in ordered:
            cost = _tok(f"- [{r['kind']}] {r['title']}: {r['body']}")
            if out and used + cost > cap_tokens:
                break
            out.append(r)
            used += cost
        return out

    def for_prompt(self, repo: str, touched=None, cap_tokens: int = 1800) -> str:
        """The block injected into a prompt. Bumps `hits` on what it injects — a memory never
        injected is decoration. Empty string when there is nothing to say."""
        rows = self.recall(repo, touched, cap_tokens)
        if not rows:
            return ""
        self.db.executemany("UPDATE memories SET hits = hits + 1 WHERE id=?",
                            [(r["id"],) for r in rows])
        self.db.commit()
        head = ["WHAT WE ALREADY KNOW ABOUT THIS REPOSITORY",
                "(learned on previous runs or stated by the repo itself — "
                "do not contradict these, and do not repeat these mistakes)"]
        return "\n".join(head + [f"- [{r['kind']}] {r['title']}: {r['body']}" for r in rows])

    def conflicts(self, repo: str) -> list[dict]:
        """Live memory pairs that share a pin, were recorded at different SHAs, and are of a kind
        that states a rule — candidates for a human to reconcile. Zero LLM calls, never
        auto-resolved: silently dropping one side (GraphRAG's move) loses the claim with no record."""
        live = [dict(r) for r in self.db.execute(
            "SELECT id, kind, title, pins, valid_from_sha FROM memories"
            " WHERE repo=? AND valid_until_sha IS NULL AND kind IN ('convention','decision','lesson')",
            (repo,))]
        by_pin: dict[str, list[dict]] = {}
        for r in live:
            for p in _pins_of(r):
                by_pin.setdefault(p, []).append(r)

        def brief(r):
            return {"id": r["id"], "kind": r["kind"], "title": r["title"], "sha": r["valid_from_sha"]}

        pairs, seen = [], set()
        for pin, rows in by_pin.items():
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    if (a["valid_from_sha"] or "") == (b["valid_from_sha"] or ""):
                        continue
                    key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append({"pin": pin, "a": brief(a), "b": brief(b)})
        return pairs


# ─── automatic capture ──────────────────────────────────────────────────────

def harvest_conventions(mem: MemoryStore, repo: str, root: Path | None,
                        sha: str | None = None) -> list[str]:
    """Read the repo's own instructions to contributors and remember them."""
    if root is None:
        return []
    found = []
    for rel in CONVENTION_FILES:
        p = root / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text("utf-8", "replace")
        except OSError:
            continue
        # Headings plus their first paragraph: enough to carry the rule, small
        # enough that a dozen of them still fit in a prompt.
        for m in re.finditer(r"^#{1,3}\s+(.{3,90})$", text, re.M):
            start = m.end()
            para = text[start:start + 700].strip().split("\n\n")[0].strip()
            if len(para) < 40:
                continue
            mem.add(repo, "convention", f"{rel}: {m.group(1).strip()}",
                    " ".join(para.split())[:900], source=f"file:{rel}",
                    pins=[f"file:{rel}"], sha=sha)
            found.append(m.group(1).strip())
        if not found:
            mem.add(repo, "convention", f"{rel}",
                    " ".join(text.split())[:900], source=f"file:{rel}",
                    pins=[f"file:{rel}"], sha=sha)
            found.append(rel)
    return found


def remember_rejection(mem: MemoryStore, repo: str, run_id: str, goal: str,
                       note: str, sha: str | None = None) -> None:
    """The single most valuable sentence in the system: why a human said no."""
    if not note or note == "no reason given":
        return
    mem.add(repo, "lesson", f"Rejected: {goal[:110]}",
            f"A human rejected this change because: {note.strip()[:600]}",
            source=f"run:{run_id}", run_id=run_id, sha=sha)


def remember_verify_problems(mem: MemoryStore, repo: str, run_id: str,
                             problems: list[dict], sha: str | None = None) -> None:
    for p in problems[:5]:
        f = p.get("file")
        mem.add(repo, "gotcha",
                f"{p.get('problem', 'problem')} in {f or '?'}",
                f"A previous run produced {p.get('problem')} in "
                f"{f}. Details: {str(p)[:400]}",
                source=f"run:{run_id}", run_id=run_id,
                pins=[f"file:{f}"] if f else None, sha=sha)
