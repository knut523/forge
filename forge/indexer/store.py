"""SQLite store for the code index.

SQLite on purpose: the index must be a single file an engineer run can copy,
diff, or throw away, with no server to keep alive. It also keeps this service
genuinely independent — nothing here reaches into the platform's Postgres,
Qdrant, or Valkey.

The graph is three tables — `symbols` (nodes), `refs` (call/usage edges), and
`imports` (file-level edges). Everything the query layer answers is a join over
those, so there is no separate graph engine to keep in sync with the source.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    root         TEXT NOT NULL,
    head_sha     TEXT,
    branch       TEXT,
    indexed_at   TEXT,
    file_count   INTEGER DEFAULT 0,
    symbol_count INTEGER DEFAULT 0,
    ref_count    INTEGER DEFAULT 0,
    resolved_pct REAL    DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    lang        TEXT NOT NULL,
    sha         TEXT NOT NULL,
    loc         INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    parse_error TEXT,
    UNIQUE (repo_id, path)
);
CREATE INDEX IF NOT EXISTS ix_files_repo ON files(repo_id);

CREATE TABLE IF NOT EXISTS symbols (
    id         INTEGER PRIMARY KEY,
    repo_id    INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    qualname   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    start_line INTEGER,
    end_line   INTEGER,
    signature  TEXT,
    doc        TEXT,
    exported   INTEGER DEFAULT 0,
    parent     TEXT
);
CREATE INDEX IF NOT EXISTS ix_symbols_name ON symbols(repo_id, name);
CREATE INDEX IF NOT EXISTS ix_symbols_qual ON symbols(repo_id, qualname);
CREATE INDEX IF NOT EXISTS ix_symbols_file ON symbols(file_id);

CREATE TABLE IF NOT EXISTS imports (
    id               INTEGER PRIMARY KEY,
    repo_id          INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file_id          INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    module           TEXT NOT NULL,
    symbol           TEXT,
    alias            TEXT,
    line             INTEGER,
    resolved_file_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS ix_imports_res  ON imports(resolved_file_id);

CREATE TABLE IF NOT EXISTS refs (
    id            INTEGER PRIMARY KEY,
    repo_id       INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    src_qual      TEXT,
    src_symbol_id INTEGER,
    name          TEXT NOT NULL,
    full          TEXT,
    line          INTEGER,
    kind          TEXT,
    dst_symbol_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_refs_dst  ON refs(dst_symbol_id);
CREATE INDEX IF NOT EXISTS ix_refs_src  ON refs(src_symbol_id);
CREATE INDEX IF NOT EXISTS ix_refs_name ON refs(repo_id, name);
CREATE INDEX IF NOT EXISTS ix_refs_file ON refs(file_id);

-- URL paths seen in source. The cross-repo seam: a symbol graph cannot connect
-- a frontend fetch to the backend route it calls, but the string can.
CREATE TABLE IF NOT EXISTS literals (
    id      INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    value   TEXT NOT NULL,
    line    INTEGER,
    kind    TEXT NOT NULL DEFAULT 'path'
);
CREATE INDEX IF NOT EXISTS ix_lit_value ON literals(value);
CREATE INDEX IF NOT EXISTS ix_lit_repo  ON literals(repo_id);
CREATE INDEX IF NOT EXISTS ix_lit_file  ON literals(file_id);

-- Not contentless: a contentless fts5 table rejects DELETE, and re-indexing a
-- repo has to remove that repo's old rows without touching any other repo's.
-- The duplicated text costs a few MB and buys a simple, correct write path.
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts
    USING fts5(name, qualname, signature, doc);
"""


class Store:
    def __init__(self, db_path: str | Path, readonly: bool = False):
        """`readonly` opens the index without touching it at all.

        The API serves queries this way, off a read-only mount: a query service
        has no business creating schema, and a WAL pragma against a read-only
        file fails outright. Writers (the indexer) use the default path.
        """
        self.path = Path(db_path)
        self.readonly = readonly
        if readonly:
            if not self.path.exists():
                raise ValueError(f"no index at {self.path} — index a repo first")
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._migrate_fts()
        self.db.executescript(SCHEMA)

    def _migrate_fts(self) -> None:
        """Replace a pre-existing contentless symbols_fts, rebuilding its rows.

        Early indexes were written with `content=''`, which makes the table
        read-only for deletes and breaks re-indexing. Detect and repair rather
        than making the operator delete the database by hand.
        """
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='symbols_fts'").fetchone()
        if row is None or "content=''" not in (row[0] or ""):
            return
        self.db.execute("DROP TABLE symbols_fts")
        self.db.execute(
            "CREATE VIRTUAL TABLE symbols_fts USING fts5(name, qualname, signature, doc)")
        self.db.execute(
            "INSERT INTO symbols_fts (rowid, name, qualname, signature, doc)"
            " SELECT id, name, qualname, COALESCE(signature,''), COALESCE(doc,'')"
            "   FROM symbols")
        self.db.commit()

    def close(self) -> None:
        if not self.readonly:
            self.db.commit()
        self.db.close()

    # ─── write path ─────────────────────────────────────────────────────────

    def reset_repo(self, name: str, root: str, head_sha: str | None,
                   branch: str | None) -> int:
        """Drop any previous index for this repo and return a fresh repo id.

        A full re-index rather than an incremental one: at this size it takes
        seconds, and a stale edge is far more damaging to an engineer run than
        a slow index.
        """
        row = self.db.execute("SELECT id FROM repos WHERE name = ?", (name,)).fetchone()
        if row is not None:
            old = row["id"]
            self.db.execute(
                "DELETE FROM symbols_fts WHERE rowid IN "
                "(SELECT id FROM symbols WHERE repo_id = ?)", (old,))
            self.db.execute("DELETE FROM repos WHERE id = ?", (old,))
        cur = self.db.execute(
            "INSERT INTO repos (name, root, head_sha, branch) VALUES (?,?,?,?)",
            (name, root, head_sha, branch))
        self.db.commit()
        return int(cur.lastrowid)

    def add_file(self, repo_id: int, path: str, lang: str, sha: str,
                 loc: int, size: int, parse_error: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO files (repo_id, path, lang, sha, loc, bytes, parse_error) "
            "VALUES (?,?,?,?,?,?,?)",
            (repo_id, path, lang, sha, loc, size, parse_error))
        return int(cur.lastrowid)

    def add_symbols(self, repo_id: int, file_id: int, symbols: Iterable[Any]) -> None:
        rows = [(repo_id, file_id, s.name, s.qualname, s.kind, s.start_line,
                 s.end_line, s.signature, s.doc, int(s.exported), s.parent)
                for s in symbols]
        if not rows:
            return
        self.db.executemany(
            "INSERT INTO symbols (repo_id, file_id, name, qualname, kind, start_line,"
            " end_line, signature, doc, exported, parent) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        # Read the ids back rather than assuming they are contiguous — the FTS
        # rowid must match the symbol id exactly or every search points at the
        # wrong definition.
        back = self.db.execute(
            "SELECT id, name, qualname, signature, doc FROM symbols WHERE file_id = ?",
            (file_id,)).fetchall()
        self.db.executemany(
            "INSERT INTO symbols_fts (rowid, name, qualname, signature, doc) VALUES (?,?,?,?,?)",
            [(r["id"], r["name"], r["qualname"], r["signature"] or "", r["doc"] or "")
             for r in back])

    def add_imports(self, repo_id: int, file_id: int, imports: Iterable[Any]) -> None:
        rows = [(repo_id, file_id, i.module, i.symbol, i.alias, i.line)
                for i in imports]
        if rows:
            self.db.executemany(
                "INSERT INTO imports (repo_id, file_id, module, symbol, alias, line) "
                "VALUES (?,?,?,?,?,?)", rows)

    def add_refs(self, repo_id: int, file_id: int, refs: Iterable[Any]) -> None:
        rows = [(repo_id, file_id, r.src, r.name, r.full, r.line, r.kind)
                for r in refs]
        if rows:
            # src is stored as a qualname and turned into an id by the resolve
            # pass, so the writer never needs to know symbol ids up front.
            self.db.executemany(
                "INSERT INTO refs (repo_id, file_id, src_qual, name, full, line, kind) "
                "VALUES (?,?,?,?,?,?,?)", rows)

    def add_literals(self, repo_id: int, file_id: int, literals: Iterable[Any]) -> None:
        rows = [(repo_id, file_id, x.value, x.line, x.kind) for x in literals]
        if rows:
            self.db.executemany(
                "INSERT INTO literals (repo_id, file_id, value, line, kind) "
                "VALUES (?,?,?,?,?)", rows)

    def commit(self) -> None:
        self.db.commit()

    def finalize(self, repo_id: int, indexed_at: str) -> dict:
        q = self.db.execute
        files = q("SELECT COUNT(*) FROM files WHERE repo_id=?", (repo_id,)).fetchone()[0]
        syms = q("SELECT COUNT(*) FROM symbols WHERE repo_id=?", (repo_id,)).fetchone()[0]
        refs = q("SELECT COUNT(*) FROM refs WHERE repo_id=?", (repo_id,)).fetchone()[0]
        res = q("SELECT COUNT(*) FROM refs WHERE repo_id=? AND dst_symbol_id IS NOT NULL",
                (repo_id,)).fetchone()[0]
        pct = round(100.0 * res / refs, 1) if refs else 0.0
        q("UPDATE repos SET indexed_at=?, file_count=?, symbol_count=?, ref_count=?,"
          " resolved_pct=? WHERE id=?", (indexed_at, files, syms, refs, pct, repo_id))
        self.db.commit()
        journal = self._seal()
        return {"files": files, "symbols": syms, "refs": refs,
                "resolved": res, "resolved_pct": pct, "journal": journal}

    def _seal(self) -> str:
        """Leave the index as one self-contained, read-only-friendly file.

        WAL is the right mode *while* indexing, but it leaves -wal/-shm side
        files and a header that makes a read-only open fail: SQLite must create
        the -shm file even to read, which a read-only mount forbids. Checkpoint
        and drop back to a rollback journal so the API — and anyone copying the
        index around — gets a single file that just opens.

        Done on a fresh connection: the switch is refused while any other
        connection holds the database, and — the part that bit — `PRAGMA
        journal_mode` reports the mode still in force instead of raising, so a
        refusal is silent. Hence the explicit read-back.
        """
        self.db.commit()
        self.db.close()
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            con.commit()
        finally:
            con.close()
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        return mode
