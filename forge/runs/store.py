"""Run records — what the coder did, in enough detail to be reviewed.

A run is a sequence of phases; each phase emits events; some events carry
artifacts. The view is a direct projection of this table, which is the point:
if something happened and the view cannot show it, the gap is here, not in the
UI.

Lives in the writable config volume next to credentials, not in the code index —
the index is disposable and gets rebuilt, run history must not.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The full arc of a repo-contributing run. Phases beyond `plan` are declared
# here before they are implemented on purpose: the view renders the whole road
# and greys out what does not exist yet, so nobody mistakes a short run for a
# complete one.
PHASES = [
    ("capture", "Capture",  "Read the codebase: structure, hotspots, entry points"),
    ("impact",  "Impact",   "Wayfinder — what else does this touch, across every repo"),
    ("plan",    "Plan",     "Spec and ordered steps, grounded in what Capture found"),
    ("build",   "Build",    "Write the code on a branch"),
    ("verify",  "Verify",   "Tests, typecheck, acceptance criteria"),
    ("review",  "Review",   "Screenshots and diff, assembled for a human"),
    ("approve", "Approve",  "A human decides — nothing reaches GitHub before this"),
    ("pr",      "PR",       "Push the branch and open the pull request"),
]
IMPLEMENTED = {"capture", "impact", "plan", "build", "verify"}

# Terminal states, chosen so none of them can be read as "the goal was reached"
# when it wasn't. A run that merely ran out of implemented phases is NOT done.
TERMINAL = {
    "complete":   "every phase ran and the work is ready for a human",
    "incomplete": "ran out of road — the remaining phases are not built yet",
    "blocked":    "a phase could not do its job and the run stopped there",
    "failed":     "an error ended the run",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         TEXT PRIMARY KEY,
    repo       TEXT NOT NULL,
    goal       TEXT NOT NULL,
    target     TEXT,
    status     TEXT NOT NULL,
    phase      TEXT,
    model      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error      TEXT,
    stopped_reason TEXT,
    next_action    TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_repo ON runs(repo, created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    run_id  TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    phase   TEXT,
    kind    TEXT NOT NULL,
    title   TEXT NOT NULL,
    detail  TEXT,
    level   TEXT DEFAULT 'info'
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get(
            "FORGE_RUNS_DB", "/config/forge-runs.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first runs were recorded."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col in ("stopped_reason", "next_action"):
            if col not in have:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # ─── writes ─────────────────────────────────────────────────────────────

    def create(self, repo: str, goal: str, target: str | None,
               model: str | None) -> str:
        rid = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO runs (id, repo, goal, target, status, phase, model,"
            " created_at, updated_at) VALUES (?,?,?,?,'queued',NULL,?,?,?)",
            (rid, repo, goal, target, model, _now(), _now()))
        self.db.commit()
        return rid

    def emit(self, run_id: str, phase: str, kind: str, title: str,
             detail=None, level: str = "info") -> None:
        seq = self.db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?",
            (run_id,)).fetchone()[0]
        self.db.execute(
            "INSERT INTO events (run_id, seq, ts, phase, kind, title, detail, level)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, seq, _now(), phase, kind, title,
             json.dumps(detail, default=str) if detail is not None else None, level))
        self.db.execute("UPDATE runs SET updated_at=? WHERE id=?", (_now(), run_id))
        self.db.commit()

    def set_phase(self, run_id: str, phase: str) -> None:
        self.db.execute(
            "UPDATE runs SET phase=?, status='running', updated_at=? WHERE id=?",
            (phase, _now(), run_id))
        self.db.commit()

    def finish(self, run_id: str, status: str, error: str | None = None,
               reason: str | None = None, next_action: str | None = None) -> None:
        """A terminal state always carries WHY it stopped and what unblocks it.

        Without that, a run that halted after Plan looks identical to one that
        shipped a PR — which is exactly the confusion this replaced.
        """
        self.db.execute(
            "UPDATE runs SET status=?, error=?, stopped_reason=?, next_action=?,"
            " finished_at=?, updated_at=? WHERE id=?",
            (status, error, reason, next_action, _now(), _now(), run_id))
        self.db.commit()

    # ─── reads ──────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["counts"] = {
            k: n for k, n in self.db.execute(
                "SELECT level, COUNT(*) FROM events WHERE run_id=? GROUP BY level",
                (run_id,))}
        return d

    def list(self, repo: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT * FROM runs"
        args: list = []
        if repo:
            q += " WHERE repo=?"
            args.append(repo)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(q, args)]

    def events(self, run_id: str, after: int = 0, limit: int = 1000) -> list[dict]:
        rows = self.db.execute(
            "SELECT seq, ts, phase, kind, title, detail, level FROM events"
            " WHERE run_id=? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    def delete(self, run_id: str) -> bool:
        cur = self.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
        self.db.commit()
        return cur.rowcount > 0
