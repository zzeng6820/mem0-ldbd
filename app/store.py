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
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            for name, kind in (("receipt", "TEXT"), ("receipt_origin", "TEXT"),
                               ("part_request_ids", "TEXT"),
                               ("reconcile_checked_at", "REAL NOT NULL DEFAULT 0"),
                               ("reconciled_at", "REAL"),
                               ("confirmed_event_ids", "TEXT"),
                               ("reconcile_event_cursor", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE requests ADD COLUMN {name} {kind}")
            db.execute("CREATE INDEX IF NOT EXISTS requests_reconcile "
                       "ON requests(reconcile_checked_at, updated_at) "
                       "WHERE state != 'completed' AND event_ids IS NOT NULL")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, user_id: str, request_id: str, fingerprint: str, receipt=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?",
                             (user_id, request_id)).fetchone()
            if row:
                result = dict(row)
                if (result["fingerprint"] == fingerprint and receipt
                        and (not result["receipt"] or (result["receipt_origin"] == "event"
                                                      and result["state"] != "completed"))):
                    previous = json.loads(result["receipt"]) if result["receipt"] else None
                    if previous and any(previous.get(key) != receipt.get(key)
                                        for key in ("user_id", "request_id", "session_id")):
                        # The original request is authoritative. Do not reuse
                        # confirmations derived for a conflicting event session.
                        result["confirmed_event_ids"] = "[]"
                        result["reconcile_event_cursor"] = 0
                    result["receipt"] = json.dumps(receipt, ensure_ascii=False)
                    result["receipt_origin"] = "request"
                    db.execute("UPDATE requests SET receipt=?,receipt_origin='request' "
                               ",confirmed_event_ids=?,reconcile_event_cursor=? "
                               "WHERE user_id=? AND request_id=?",
                               (result["receipt"], result["confirmed_event_ids"],
                                result["reconcile_event_cursor"], user_id, request_id))
                return result, False
            encoded = json.dumps(receipt, ensure_ascii=False) if receipt else None
            origin = "request" if receipt else None
            db.execute("INSERT INTO requests(user_id,request_id,fingerprint,state,updated_at,receipt,receipt_origin) "
                       "VALUES(?,?,?,'submitted',?,?,?)",
                       (user_id, request_id, fingerprint, time.time(), encoded, origin))
            return {"state": "submitted", "fingerprint": fingerprint, "receipt": encoded,
                    "receipt_origin": origin}, True

    def update(self, user_id: str, request_id: str, state: str, **fields):
        allowed = {"event_ids", "response", "error", "status_code", "receipt", "reconciled_at"}
        if fields.keys() - allowed:
            raise ValueError("Unsupported ledger field")
        values = {"state": state, "updated_at": time.time(), **fields}
        for key in ("event_ids", "response", "receipt"):
            if key in values:
                values[key] = json.dumps(values[key], ensure_ascii=False)
        with self.connect() as db:
            result = db.execute("UPDATE requests SET " + ",".join(f"{k}=?" for k in values) +
                                " WHERE user_id=? AND request_id=? AND state != 'completed'",
                                (*values.values(), user_id, request_id))
            return result.rowcount > 0

    def complete(self, user_id, request_id, response, *, reconciled=False):
        fields = {"response": response, "error": None, "status_code": None}
        if reconciled:
            fields["reconciled_at"] = time.time()
        self.update(user_id, request_id, "completed", **fields)
        row = self.get(user_id, request_id)
        if not row or row["state"] != "completed" or not row["response"]:
            raise RuntimeError("Cannot complete an unclaimed request")
        return json.loads(row["response"])

    def reconciliation_candidates(self, limit=100, request_ids=None):
        where = ("state != 'completed' AND ((event_ids IS NOT NULL AND event_ids != '[]') "
                 "OR part_request_ids IS NOT NULL)")
        args = []
        if request_ids is not None:
            if not request_ids:
                return []
            where += " AND request_id IN (" + ",".join("?" for _ in request_ids) + ")"
            args.extend(request_ids)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM requests WHERE " + where +
                " ORDER BY reconcile_checked_at, updated_at, user_id, request_id LIMIT ?",
                (*args, limit))]

    def touch_reconciliation(self, user_id, request_id):
        with self.connect() as db:
            db.execute("UPDATE requests SET reconcile_checked_at=? "
                       "WHERE user_id=? AND request_id=?", (time.time(), user_id, request_id))

    def set_part_requests(self, user_id, request_id, part_ids):
        if not part_ids or len(set(part_ids)) != len(part_ids) or request_id in part_ids:
            raise ValueError("Invalid partition plan")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT part_request_ids FROM requests WHERE user_id=? AND request_id=?",
                             (user_id, request_id)).fetchone()
            if not row:
                raise ValueError("Partition parent was not claimed")
            if row[0] and json.loads(row[0]) != part_ids:
                raise ValueError("Partition plan conflicts with persisted request")
            db.execute("UPDATE requests SET part_request_ids=? WHERE user_id=? AND request_id=?",
                       (json.dumps(part_ids), user_id, request_id))

    def completed_parts_receipt(self, user_id, request_id):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?",
                             (user_id, request_id)).fetchone()
            if not row or not row["part_request_ids"] or not row["receipt"]:
                return None
            part_ids = json.loads(row["part_request_ids"])
            for part in part_ids:
                child = db.execute("SELECT state FROM requests WHERE user_id=? AND request_id=?",
                                   (user_id, part)).fetchone()
                if not child or child["state"] != "completed":
                    return None
            return json.loads(row["receipt"])

    def record_event_check(self, user_id, request_id, event_id, *, receipt=None, completed=False):
        """Persist read-only event progress without changing the request outcome."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = dict(db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?",
                                  (user_id, request_id)).fetchone())
            if row["state"] == "completed":
                return row
            events = list(dict.fromkeys(json.loads(row["event_ids"] or "[]")))
            if event_id not in events:
                raise ValueError("Event is not bound to this request")
            saved = json.loads(row["receipt"]) if row["receipt"] else None
            if receipt is not None:
                if (receipt.get("success") is not True or receipt.get("user_id") != user_id
                        or receipt.get("request_id") != request_id
                        or not isinstance(receipt.get("session_id"), str)
                        or not receipt["session_id"].strip()):
                    raise ValueError("Invalid event receipt")
                if saved and any(saved.get(key) != receipt.get(key)
                                 for key in ("user_id", "request_id", "session_id")):
                    raise ValueError("Conflicting event session")
                saved = saved or receipt
            confirmed = json.loads(row["confirmed_event_ids"] or "[]")
            if completed:
                if saved is None:
                    raise ValueError("Confirmed event requires a verified receipt")
                if event_id not in confirmed:
                    confirmed.append(event_id)
            origin = row["receipt_origin"] or ("event" if receipt is not None else None)
            values = {"receipt": json.dumps(saved, ensure_ascii=False) if saved else None,
                      "receipt_origin": origin,
                      "confirmed_event_ids": json.dumps(confirmed),
                      "reconcile_event_cursor": (events.index(event_id) + 1) % len(events)}
            db.execute("UPDATE requests SET receipt=?,receipt_origin=?,confirmed_event_ids=?,reconcile_event_cursor=? "
                       "WHERE user_id=? AND request_id=?", (*values.values(), user_id, request_id))
            return {**row, **values}

    def get(self, user_id: str, request_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?",
                             (user_id, request_id)).fetchone()
            return dict(row) if row else None
