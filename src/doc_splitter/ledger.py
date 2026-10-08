import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    generation INTEGER NOT NULL,
    source_name TEXT NOT NULL,
    source_signature TEXT NOT NULL,
    config_revision TEXT NOT NULL,
    config TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    proposal TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    UNIQUE(source_hash, generation)
);
CREATE TABLE IF NOT EXISTS outputs (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    name TEXT NOT NULL UNIQUE,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    PRIMARY KEY(job_id, start)
);
CREATE TABLE IF NOT EXISTS observations (
    name TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    since REAL NOT NULL,
    handled INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    retryable INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    time REAL NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS input_cleanups (
    source_name TEXT NOT NULL,
    source_signature TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    directory TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY(source_name, source_signature)
);
"""


class Ledger:
    def __init__(self, path: Path):
        if path.is_symlink():
            raise ValueError("ledger must not be a symlink")
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            raise ValueError("unsupported ledger schema; restore matching application image")
        self.db.executescript(SCHEMA)
        with self.db:
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(observations)")}
            for column in ("attempts", "retryable"):
                if column not in columns:
                    self.db.execute(
                        f"ALTER TABLE observations ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            self.db.execute(
                "INSERT OR IGNORE INTO input_cleanups(source_name,source_signature,source_hash) "
                "SELECT source_name,source_signature,source_hash FROM jobs"
            )
            self.db.execute("PRAGMA user_version=3")

    def close(self):
        self.db.close()

    def job(self, job_id: str):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("unknown job")
        return row

    def outputs(self, job_id: str):
        return self.db.execute(
            "SELECT * FROM outputs WHERE job_id=? ORDER BY start", (job_id,)
        ).fetchall()

    def event(self, job_id: str, event: str, detail: str = ""):
        self.db.execute(
            "INSERT INTO events(job_id,time,event,detail) VALUES(?,?,?,?)",
            (job_id, time.time(), event, detail),
        )

    def register_input(self, name: str, signature: str, source_hash: str):
        self.db.execute(
            "INSERT OR IGNORE INTO input_cleanups(source_name,source_signature,source_hash) "
            "VALUES(?,?,?)",
            (name, signature, source_hash),
        )

    def transition(self, job_id: str, status: str, error: str | None = None):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,error=?,updated=? WHERE id=?",
                (status, error, time.time(), job_id),
            )
            self.event(job_id, status, error or "")

    def manifest(self, job_id: str) -> dict:
        row = dict(self.job(job_id))
        for name in ("config", "proposal", "source_signature"):
            row[name] = json.loads(row[name]) if row[name] else None
        row["outputs"] = [dict(output) for output in self.outputs(job_id)]
        row["events"] = [
            dict(event)
            for event in self.db.execute(
                "SELECT time,event,detail FROM events WHERE job_id=? ORDER BY id", (job_id,)
            )
        ]
        row["schema"] = 1
        return row
