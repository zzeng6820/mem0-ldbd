"""Durable request ledger. Conversation content and API keys are never stored here."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class RequestStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                event_ids TEXT, response TEXT, error TEXT, status_code INTEGER,
                updated_at REAL NOT NULL, PRIMARY KEY(user_id, request_id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, user_id: str, request_id: str, fingerprint: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?",
                             (user_id, request_id)).fetchone()
            if row:
                return dict(row), False
            db.execute("INSERT INTO requests(user_id,request_id,fingerprint,state,updated_at) "
                       "VALUES(?,?,?,'submitted',?)", (user_id, request_id, fingerprint, time.time()))
            return {"state": "submitted", "fingerprint": fingerprint}, True

    def update(self, user_id: str, request_id: str, state: str, **fields):
        allowed = {"event_ids", "response", "error", "status_code"}
        if fields.keys() - allowed:
            raise ValueError("Unsupported ledger field")
        values = {"state": state, "updated_at": time.time(), **fields}
        for key in ("event_ids", "response"):
            if key in values:
                values[key] = json.dumps(values[key], ensure_ascii=False)
        with self.connect() as db:
            db.execute("UPDATE requests SET " + ",".join(f"{k}=?" for k in values) +
                       " WHERE user_id=? AND request_id=?", (*values.values(), user_id, request_id))

    def get(self, user_id: str, request_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?",
                             (user_id, request_id)).fetchone()
            return dict(row) if row else None
