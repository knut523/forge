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
    UNIQUE (repo, kind, title)
);
CREATE INDEX IF NOT EXISTS ix_mem_repo ON memories(repo, active);
"""

KINDS = ("convention", "lesson", "gotcha", "decision")

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

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def add(self, repo: str, kind: str, title: str, body: str,
            source: str = "human", run_id: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        title, body = title.strip()[:200], body.strip()[:4000]
        if not title or not body:
            raise ValueError("title and body are required")
        # Re-learning the same lesson should refresh it, not duplicate it.
        self.db.execute(
            "INSERT INTO memories (repo, kind, title, body, source, run_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(repo, kind, title) DO UPDATE SET body=excluded.body,"
            "   source=excluded.source, run_id=excluded.run_id, active=1",
            (repo, kind, title, body, source, run_id, _now()))
        self.db.commit()
        return self.get(repo, kind, title)

    def get(self, repo: str, kind: str, title: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM memories WHERE repo=? AND kind=? AND title=?",
            (repo, kind, title)).fetchone()
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

    def for_prompt(self, repo: str, limit: int = 25) -> str:
        """The block injected into Plan and Build. Empty string when there is
        nothing to say — an empty heading trains the model to ignore it."""
        rows = self.db.execute(
            "SELECT id, kind, title, body FROM memories"
            " WHERE repo=? AND active=1"
            " ORDER BY CASE kind WHEN 'gotcha' THEN 0 WHEN 'lesson' THEN 1"
            "                    WHEN 'convention' THEN 2 ELSE 3 END, id DESC"
            " LIMIT ?", (repo, limit)).fetchall()
        if not rows:
            return ""
        self.db.executemany("UPDATE memories SET hits = hits + 1 WHERE id=?",
                            [(r["id"],) for r in rows])
        self.db.commit()
        out = ["WHAT WE ALREADY KNOW ABOUT THIS REPOSITORY",
               "(learned on previous runs or stated by the repo itself — "
               "do not contradict these, and do not repeat these mistakes)"]
        for r in rows:
            out.append(f"- [{r['kind']}] {r['title']}: {r['body']}")
        return "\n".join(out)


# ─── automatic capture ──────────────────────────────────────────────────────

def harvest_conventions(mem: MemoryStore, repo: str, root: Path | None) -> list[str]:
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
                    " ".join(para.split())[:900], source=f"file:{rel}")
            found.append(m.group(1).strip())
        if not found:
            mem.add(repo, "convention", f"{rel}",
                    " ".join(text.split())[:900], source=f"file:{rel}")
            found.append(rel)
    return found


def remember_rejection(mem: MemoryStore, repo: str, run_id: str, goal: str,
                       note: str) -> None:
    """The single most valuable sentence in the system: why a human said no."""
    if not note or note == "no reason given":
        return
    mem.add(repo, "lesson", f"Rejected: {goal[:110]}",
            f"A human rejected this change because: {note.strip()[:600]}",
            source=f"run:{run_id}", run_id=run_id)


def remember_verify_problems(mem: MemoryStore, repo: str, run_id: str,
                             problems: list[dict]) -> None:
    for p in problems[:5]:
        mem.add(repo, "gotcha",
                f"{p.get('problem', 'problem')} in {p.get('file', '?')}",
                f"A previous run produced {p.get('problem')} in "
                f"{p.get('file')}. Details: {str(p)[:400]}",
                source=f"run:{run_id}", run_id=run_id)
