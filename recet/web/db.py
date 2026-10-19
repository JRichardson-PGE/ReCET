"""The web app's SQLite store. One file in the state folder; a new connection per call, WAL mode, so the request threads and the run workers never share one.

Tables: users, sessions (hashed tokens), setup_links (hashed one-time tokens), login_failures (rate limits), runs, drafts, views (saved Results columns per user),
audit (sign-ins, runs, user changes).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',            -- 'admin' | 'user'
    password_hash TEXT,                           -- NULL until the person sets one from a setup link
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_links (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    ip TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,                         -- queued | running | finished | failed | cancelled | expired
    acc_version TEXT NOT NULL,
    compare_with TEXT,
    preflight_only INTEGER NOT NULL DEFAULT 0,
    inputs TEXT NOT NULL DEFAULT '',              -- the uploaded file names, for the list and the audit log
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    exit_code INTEGER,
    result_dir TEXT,                              -- relative to the run folder
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_by_user ON runs(user_id, created_at);
CREATE INDEX IF NOT EXISTS runs_by_status ON runs(status, created_at);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS views (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    keys TEXT NOT NULL,                           -- JSON list of column keys
    PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS value_list_changes (     -- admin changes over the shipped CET value lists (recet/web/value_lists.py)
    list TEXT NOT NULL,
    code TEXT NOT NULL,
    kind TEXT NOT NULL,                           -- added | retired | reinstated
    start_year INTEGER,
    end_year INTEGER,
    description TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    email TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL,
    PRIMARY KEY (list, code)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    user_id INTEGER,
    email TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
        finally:
            c.close()

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute(sql, args).fetchone()

    def all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(sql, args).fetchall()

    def run(self, sql: str, args=()) -> int:
        """Execute one statement; returns the number of rows changed (or the new row id for an INSERT)."""
        with self.connect() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def audit(self, event: str, user=None, detail: str = "", ip: str = "") -> None:
        self.run("INSERT INTO audit (at, user_id, email, event, detail, ip) VALUES (?,?,?,?,?,?)",
                 (time.time(), user["id"] if user else None, user["email"] if user else "", event, detail[:2000], ip))
