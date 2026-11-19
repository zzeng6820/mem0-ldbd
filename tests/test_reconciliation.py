"""Durable reconciliation tests; all Mem0 responses are synthetic and in-process."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.reconciliation import EventReconciler
from app.store import RequestStore


AUTH = {"X-Api-Key": "reconciliation-wrapper-test"}
ADD_PATH = "/v1/memories/"


def event(status="FAILED", *, user="reconcile-user", request="reconcile-request", session="reconcile-session"):
    return {
        "status": status,
        "payload": {"user_id": user, "run_id": session, "metadata": {"request_id": request}},
    }


class Backend:
    def __init__(self):
        self.calls = []
        self.events = {"event-1": event()}
        self.add_events = ["event-1"]
        self.add_status = 200
        self.block_get_number = None
        self.blocked = threading.Event()
        self.release = threading.Event()

    async def __call__(self, request):
        assert request.url.host == "mem0.test", "Unexpected external provider request"
        assert request.headers["authorization"] == "Token reconciliation-mem0-test"
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == ADD_PATH:
            return httpx.Response(self.add_status, json=[
                {"event_id": event_id, "status": "PENDING"} for event_id in self.add_events
            ])
        if request.method == "GET" and request.url.path.startswith("/v1/event/"):
            event_id = request.url.path.rstrip("/").split("/")[-1]
            response = copy.deepcopy(self.events[event_id])
            if self.count("GET") == self.block_get_number:
                self.blocked.set()
                assert await asyncio.to_thread(self.release.wait, 5), "Test did not release the event response"
            return httpx.Response(200, json=response)
        raise AssertionError("Unexpected Mem0 route")

    def count(self, method):
        return sum(verb == method for verb, _, _ in self.calls)


@pytest.fixture
def body():
    return {
        "request_id": "reconcile-request", "user_id": "reconcile-user", "session_id": "reconcile-session",
        "messages": [{"role": "user", "content": "I prefer tea.", "timestamp": 1704067200000}],
    }


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "reconciliation.sqlite3"


@pytest.fixture
def service_factory(ledger_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Reconciliation tests must never use a real network")

    async def no_async_network(*args, **kwargs):
        no_network()

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_network)

    @contextmanager
    def build(backend, **overrides):
        options = {
            "wrapper_api_key": "reconciliation-wrapper-test", "mem0_api_key": "reconciliation-mem0-test",
            "mem0_base_url": "https://mem0.test", "mem0_api_mode": "legacy", "db_path": str(ledger_path),
            "upstream_timeout_seconds": 10, "poll_interval_seconds": 0.005, "add_wait_seconds": 0.1,
            "reconcile_interval_seconds": 3600, "reconcile_batch_size": 100,
        }
        options.update(overrides)
        app = create_app(Settings(**options), transport=httpx.MockTransport(backend))
        with TestClient(app) as client:
            yield client

    return build


def row_at(path, user="reconcile-user", request="reconcile-request"):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?", (user, request)).fetchone()
    return dict(row) if row else None


def reconcile(client):
    return client.portal.call(client.app.state.reconciler.reconcile_once)


def assert_completed(row, body):
    assert row is not None and row["state"] == "completed"
    assert row["error"] is None and row["status_code"] is None
    response = json.loads(row["response"])
    assert response["success"] is True
    for key in ("user_id", "request_id", "session_id"):
        assert response[key] == body[key]
    return response


def test_failed_event_later_succeeded_is_rechecked_on_retry_without_second_add(
    service_factory, backend, body, ledger_path
):
    with service_factory(backend) as client:
        failed = client.post("/add", headers=AUTH, json=body)
        assert failed.status_code >= 400
        before = row_at(ledger_path)
        assert before["state"] == "failed" and before["error"] and before["status_code"]
        backend.events["event-1"]["status"] = "SUCCEEDED"
        retried = client.post("/add", headers=AUTH, json=body)
        assert retried.status_code == 200, retried.text
        assert_completed(row_at(ledger_path), body)
        assert backend.count("POST") == 1
        assert backend.count("GET") >= 2


def test_background_loop_recovers_success_without_any_client_retry(service_factory, backend, body, ledger_path):
    with service_factory(backend, reconcile_interval_seconds=0.02) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        backend.events["event-1"]["status"] = "SUCCEEDED"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if row_at(ledger_path)["state"] == "completed":
                break
            time.sleep(0.01)
        assert_completed(row_at(ledger_path), body)
        assert backend.count("POST") == 1
        assert backend.count("GET") >= 2


def test_restart_reconciliation_preserves_receipt_session_and_image_warnings(
    service_factory, backend, body, ledger_path
):
    body["session_id"] = "original-session-with-images"
    body["messages"][0]["content"] = [
        {"type": "text", "text": "I prefer tea."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGljLWltYWdl"}},
    ]
    backend.events["event-1"]["payload"]["run_id"] = body["session_id"]
    with service_factory(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        saved = row_at(ledger_path)
        template = json.loads(saved["receipt"])
        assert template["session_id"] == body["session_id"]
        assert template["ignored_image_parts"] == 1 and template["warnings"]
    backend.events["event-1"]["status"] = "SUCCEEDED"
    with service_factory(backend) as client:
        reconcile(client)
        result = assert_completed(row_at(ledger_path), body)
        assert result["ignored_image_parts"] == 1 and result["warnings"] == template["warnings"]
        assert client.post("/add", headers=AUTH, json=body).json() == result
    assert backend.count("POST") == 1
    assert json.loads(row_at(ledger_path)["event_ids"]) == ["event-1"]


def create_old_ledger(path):
    """Build the real pre-migration schema, including an unrelated sentinel row."""
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE requests (
            user_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, event_ids TEXT, response TEXT, error TEXT, status_code INTEGER,
            updated_at REAL NOT NULL, PRIMARY KEY(user_id, request_id))""")
        db.executemany("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)", [
            ("reconcile-user", "reconcile-request", "legacy-fingerprint", "failed", '["event-1"]',
             None, "Original extraction failure", 502, 1.0),
            ("untouched-user", "untouched-request", "sentinel-fingerprint", "failed", None,
             None, "Keep this unrelated ledger entry", 400, 2.0),
        ])


@pytest.mark.parametrize("invalid_owner", [None, "user_id", "request_id", "missing_payload", "missing_run_id"])
def test_old_schema_migrates_without_data_loss_and_recovers_only_verified_event_owner(
    service_factory, backend, body, ledger_path, invalid_owner
):
    create_old_ledger(ledger_path)
    backend.events["event-1"] = event("SUCCEEDED", session="session-recovered-from-event")
    if invalid_owner == "user_id":
        backend.events["event-1"]["payload"]["user_id"] = "another-user"
    elif invalid_owner == "request_id":
        backend.events["event-1"]["payload"]["metadata"]["request_id"] = "another-request"
    elif invalid_owner == "missing_payload":
        backend.events["event-1"].pop("payload")
    elif invalid_owner == "missing_run_id":
        backend.events["event-1"]["payload"].pop("run_id")
    sentinel_before = row_at(ledger_path, "untouched-user", "untouched-request")
    with service_factory(backend) as client:
        reconcile(client)
        result = row_at(ledger_path)
        if invalid_owner is None:
            expected = {**body, "session_id": "session-recovered-from-event"}
            assert_completed(result, expected)
        else:
            assert result["state"] != "completed" and result["response"] is None
    sentinel_after = row_at(ledger_path, "untouched-user", "untouched-request")
    assert {key: sentinel_after[key] for key in sentinel_before} == sentinel_before
    assert result["fingerprint"] == "legacy-fingerprint"
    with sqlite3.connect(ledger_path) as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
    assert backend.count("POST") == 0
    assert backend.count("GET") >= 1


def test_receipt_session_prevents_completing_an_event_for_another_session(
    service_factory, backend, body, ledger_path
):
    with service_factory(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        backend.events["event-1"] = event("SUCCEEDED", session="unrelated-session")
        reconcile(client)
        assert row_at(ledger_path)["state"] != "completed"
        assert row_at(ledger_path)["response"] is None
    assert backend.count("POST") == 1


@pytest.mark.parametrize("other_status", ["PENDING", "FAILED", "UNKNOWN_STATUS"])
def test_every_event_must_succeed_before_reconciliation_completes(
    service_factory, backend, body, ledger_path, other_status
):
    backend.add_events = ["event-1", "event-2"]
    backend.events["event-2"] = event()
    with service_factory(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        backend.events["event-1"]["status"] = "SUCCEEDED"
        backend.events["event-2"]["status"] = other_status
        reconcile(client)
        assert row_at(ledger_path)["state"] != "completed"
        backend.events["event-2"]["status"] = "SUCCEEDED"
        reconcile(client)
        assert_completed(row_at(ledger_path), body)
    assert backend.count("POST") == 1
    assert json.loads(row_at(ledger_path)["event_ids"]) == ["event-1", "event-2"]


@pytest.mark.parametrize("add_status", [400, 409, 503])
def test_failures_without_event_ids_are_never_replayed_or_assumed_complete(
    service_factory, backend, body, ledger_path, add_status
):
    backend.add_status = add_status
    with service_factory(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        backend.add_status = 200
        backend.events["event-1"]["status"] = "SUCCEEDED"
        reconcile(client)
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        assert row_at(ledger_path)["state"] in {"failed", "uncertain"}
    with service_factory(backend) as client:
        reconcile(client)
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
    assert backend.count("POST") == 1 and backend.count("GET") == 0


def test_reconciler_skips_requests_with_an_active_foreground_writer(service_factory, backend, body, ledger_path):
    backend.events["event-1"]["status"] = "SUCCEEDED"
    backend.block_get_number = 1
    with service_factory(backend) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/add", headers=AUTH, json=body)
            try:
                assert backend.blocked.wait(3)
                reconcile(client)
                assert backend.count("GET") == 1, "Background reconciliation raced an active foreground poll"
                assert row_at(ledger_path)["state"] != "completed"
            finally:
                backend.release.set()
            assert pending.result(timeout=3).status_code == 200
        assert_completed(row_at(ledger_path), body)
    assert backend.count("POST") == 1


def test_stale_background_failure_cannot_overwrite_foreground_success(
    service_factory, backend, body, ledger_path
):
    with service_factory(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        backend.block_get_number = backend.count("GET") + 1
        with ThreadPoolExecutor(max_workers=1) as pool:
            background = pool.submit(reconcile, client)
            try:
                assert backend.blocked.wait(3)
                backend.events["event-1"]["status"] = "SUCCEEDED"
                recovered = client.post("/add", headers=AUTH, json=body)
                assert recovered.status_code == 200, recovered.text
                before = assert_completed(row_at(ledger_path), body)
            finally:
                backend.release.set()
            background.result(timeout=3)
        after = assert_completed(row_at(ledger_path), body)
        assert after == before == recovered.json()
    assert backend.count("POST") == 1


def test_store_completion_is_monotonic_and_clears_failure_fields(ledger_path, body):
    store = RequestStore(str(ledger_path))
    receipt = {"success": True, **{key: body[key] for key in ("user_id", "request_id", "session_id")}}
    store.claim(body["user_id"], body["request_id"], "test-fingerprint", receipt=receipt)
    store.update(body["user_id"], body["request_id"], "failed", event_ids=["event-1"],
                 error="Old error", status_code=502)
    assert store.complete(body["user_id"], body["request_id"], receipt) == receipt
    store.update(body["user_id"], body["request_id"], "failed", error="Stale error", status_code=502)
    assert_completed(row_at(ledger_path), body)
    assert store.complete(body["user_id"], body["request_id"], {**receipt, "session_id": "wrong"}) == receipt
    assert json.loads(row_at(ledger_path)["event_ids"]) == ["event-1"]


def test_candidate_batches_rotate_without_dropping_unfinished_requests(ledger_path):
    store = RequestStore(str(ledger_path))
    for index in range(3):
        store.claim("batch-user", f"request-{index}", f"fingerprint-{index}")
        store.update("batch-user", f"request-{index}", "failed", event_ids=[f"event-{index}"])
    visited = []
    for _ in range(3):
        batch = store.reconciliation_candidates(1)
        assert len(batch) == 1
        row = batch[0]
        visited.append(row["request_id"])
        store.touch_reconciliation(row["user_id"], row["request_id"])
    assert set(visited) == {"request-0", "request-1", "request-2"}
    with sqlite3.connect(ledger_path) as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3


def test_legacy_reconciliation_uses_receipt_added_while_event_get_is_in_flight(
    service_factory, backend, body, ledger_path
):
    create_old_ledger(ledger_path)
    backend.events["event-1"] = event("SUCCEEDED")
    backend.block_get_number = 1
    receipt = {
        "success": True,
        **{key: body[key] for key in ("user_id", "request_id", "session_id")},
        "ignored_image_parts": 2,
        "warnings": ["Image content was ignored in text-only mode."],
    }
    with service_factory(backend) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            background = pool.submit(reconcile, client)
            try:
                assert backend.blocked.wait(3)
                assert row_at(ledger_path)["receipt"] is None
                client.app.state.store.claim(
                    body["user_id"], body["request_id"], "legacy-fingerprint", receipt=receipt,
                )
                assert json.loads(row_at(ledger_path)["receipt"]) == receipt
            finally:
                backend.release.set()
            result = background.result(timeout=3)
        assert result["completed"] == 1
        assert assert_completed(row_at(ledger_path), body) == receipt
    assert backend.count("POST") == 0 and backend.count("GET") == 1


def test_batch_timeout_cancels_slow_inspection_without_late_completion_or_ledger_changes(ledger_path, body):
    store = RequestStore(str(ledger_path))
    receipt = {"success": True, **{key: body[key] for key in ("user_id", "request_id", "session_id")}}
    store.claim(body["user_id"], body["request_id"], "slow-fingerprint", receipt=receipt)
    store.update(body["user_id"], body["request_id"], "failed", event_ids=["slow-event"],
                 error="Original failure", status_code=502)

    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        class SlowInspector:
            finished = 0
            submissions = 0

            async def inspect_events(self, event_ids, **kwargs):
                assert event_ids == ["slow-event"]
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                self.finished += 1
                return {"completed": True, "status": "completed", "session_id": body["session_id"]}

            async def submit_add(self, payload):
                self.submissions += 1
                raise AssertionError("A reconciliation timeout must never cause an add")

        upstream = SlowInspector()
        reconciler = EventReconciler(store, upstream, batch_timeout_seconds=0.02,
                                    request_timeout_seconds=1)
        cycle = asyncio.create_task(reconciler.reconcile_once())
        await asyncio.wait_for(started.wait(), timeout=1)
        summary = await asyncio.wait_for(cycle, timeout=1)
        assert summary["timed_out"] == 1 and summary["completed"] == 0
        assert cancelled.is_set(), "The timed-out inspection was left running"
        after_timeout = row_at(ledger_path)
        assert after_timeout["state"] == "failed" and after_timeout["response"] is None
        assert after_timeout["error"] == "Original failure" and after_timeout["status_code"] == 502
        release.set()
        await asyncio.sleep(0.03)
        assert upstream.finished == 0 and upstream.submissions == 0
        assert row_at(ledger_path) == after_timeout, "A cancelled task changed the ledger after the batch returned"

    asyncio.run(scenario())


@pytest.mark.parametrize("event_count", [5, 9])
def test_large_event_lists_accumulate_confirmations_across_batches_and_restart_without_adds(
    service_factory, backend, body, ledger_path, event_count
):
    events = [f"event-{index}" for index in range(event_count)]
    backend.events = {event_id: event("SUCCEEDED") for event_id in events}
    receipt = {"success": True, **{key: body[key] for key in ("user_id", "request_id", "session_id")}}
    with service_factory(backend) as client:
        store = client.app.state.store
        store.claim(body["user_id"], body["request_id"], "many-events-fingerprint", receipt=receipt)
        store.update(body["user_id"], body["request_id"], "failed", event_ids=events,
                     error="Old extraction failure", status_code=502)
        reconcile(client)
        first = row_at(ledger_path)
        assert first["state"] != "completed"
        assert json.loads(first["confirmed_event_ids"]) == events[:4]
        assert backend.count("GET") == 4
    with service_factory(backend) as client:
        confirmed_count = 4
        while confirmed_count < event_count:
            before_calls = backend.count("GET")
            reconcile(client)
            current = row_at(ledger_path)
            actual_confirmed = json.loads(current["confirmed_event_ids"])
            confirmed_count = min(confirmed_count + 4, event_count)
            assert actual_confirmed == events[:confirmed_count]
            assert 1 <= backend.count("GET") - before_calls <= 4
            if confirmed_count < event_count:
                assert current["state"] != "completed"
        assert assert_completed(row_at(ledger_path), body) == receipt
    queried = [path.rstrip("/").split("/")[-1] for method, path, _ in backend.calls if method == "GET"]
    assert queried == events, "Already-confirmed events were queried again after restart"
    assert backend.count("POST") == 0


def test_batch_cancelled_on_first_slow_event_resumes_at_second_event_next_round(ledger_path, body):
    store = RequestStore(str(ledger_path))
    receipt = {"success": True, **{key: body[key] for key in ("user_id", "request_id", "session_id")}}
    store.claim(body["user_id"], body["request_id"], "slow-first-fingerprint", receipt=receipt)
    store.update(body["user_id"], body["request_id"], "failed", event_ids=["slow-event", "fast-event"],
                 error="Old failure", status_code=502)

    async def scenario():
        release_slow = asyncio.Event()

        class Inspector:
            def __init__(self):
                self.inspected = []
                self.cancelled = 0
                self.submissions = 0

            async def inspect_events(self, event_ids, **kwargs):
                assert len(event_ids) == 1
                self.inspected.extend(event_ids)
                if event_ids == ["slow-event"]:
                    try:
                        await release_slow.wait()
                    except asyncio.CancelledError:
                        self.cancelled += 1
                        raise
                return {"completed": True, "status": "completed", "session_id": body["session_id"]}

            async def submit_add(self, payload):
                self.submissions += 1
                raise AssertionError("Event cursor recovery must never resubmit memories")

        upstream = Inspector()
        first_reconciler = EventReconciler(store, upstream, batch_timeout_seconds=0.02,
                                          request_timeout_seconds=1)
        first = await first_reconciler.reconcile_once()
        assert first["timed_out"] == 1 and upstream.cancelled == 1
        assert upstream.inspected == ["slow-event"]
        assert row_at(ledger_path)["reconcile_event_cursor"] == 1
        assert json.loads(row_at(ledger_path)["confirmed_event_ids"]) == []
        # Reopen both components so the next position must come from SQLite.
        reopened = RequestStore(str(ledger_path))
        next_reconciler = EventReconciler(reopened, upstream, batch_timeout_seconds=0.02,
                                         request_timeout_seconds=1)
        second = await next_reconciler.reconcile_once()
        assert second["timed_out"] == 1
        assert upstream.inspected[1] == "fast-event"
        assert json.loads(row_at(ledger_path)["confirmed_event_ids"]) == ["fast-event"]
        assert row_at(ledger_path)["state"] != "completed"
        release_slow.set()
        await next_reconciler.reconcile_once()
        assert_completed(row_at(ledger_path), body)
        assert upstream.inspected.count("fast-event") == 1
        assert upstream.submissions == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_second", ["missing_payload", "different_session"])
def test_legacy_receipt_from_first_event_does_not_bypass_second_event_identity_proof(
    service_factory, backend, body, ledger_path, invalid_second
):
    create_old_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as db:
        db.execute("UPDATE requests SET event_ids=? WHERE user_id=? AND request_id=?",
                   ('["event-1", "event-2"]', body["user_id"], body["request_id"]))
    backend.events = {"event-1": event("SUCCEEDED"), "event-2": event("SUCCEEDED")}
    if invalid_second == "missing_payload":
        backend.events["event-2"].pop("payload")
    else:
        backend.events["event-2"]["payload"]["run_id"] = "another-session"
    with service_factory(backend) as client:
        reconcile(client)
        partial = row_at(ledger_path)
        assert partial["state"] != "completed" and partial["response"] is None
        assert partial["receipt_origin"] == "event"
        assert json.loads(partial["confirmed_event_ids"]) == ["event-1"]
    with service_factory(backend) as client:
        reconcile(client)
        still_partial = row_at(ledger_path)
        assert still_partial["state"] != "completed" and still_partial["response"] is None
        assert json.loads(still_partial["confirmed_event_ids"]) == ["event-1"]
        backend.events["event-2"] = event("SUCCEEDED")
        reconcile(client)
        assert_completed(row_at(ledger_path), body)
    queried = [path for method, path, _ in backend.calls if method == "GET"]
    assert queried.count("/v1/event/event-1/") == 1
    assert queried.count("/v1/event/event-2/") == 3
    assert backend.count("POST") == 0


def test_original_request_session_replaces_conflicting_legacy_receipt_and_invalidates_old_confirmations(
    service_factory, backend, body, ledger_path
):
    body["session_id"] = "original-session-B"
    # Seed the historical row with this exact request's canonical fingerprint,
    # so HTTP replay exercises identity verification rather than payload conflict.
    fingerprint = hashlib.sha256(json.dumps(
        {"payload": body, "upstream": "https://mem0.test", "mode": "legacy"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    create_old_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as db:
        db.execute("UPDATE requests SET fingerprint=?,event_ids=? WHERE user_id=? AND request_id=?",
                   (fingerprint, '["event-1", "event-2"]', body["user_id"], body["request_id"]))
    backend.events = {
        "event-1": event("SUCCEEDED", session="event-session-A"),
        "event-2": event("PENDING", session="event-session-A"),
    }
    with service_factory(backend) as client:
        reconcile(client)
        partial = row_at(ledger_path)
        assert partial["state"] != "completed" and partial["receipt_origin"] == "event"
        assert json.loads(partial["receipt"])["session_id"] == "event-session-A"
        assert json.loads(partial["confirmed_event_ids"]) == ["event-1"]

        first_retry = client.post("/add", headers=AUTH, json=body)
        assert first_retry.status_code == 502, first_retry.text
        rejected = row_at(ledger_path)
        assert rejected["receipt_origin"] == "request"
        assert json.loads(rejected["receipt"])["session_id"] == body["session_id"]
        assert json.loads(rejected["confirmed_event_ids"]) == []
        assert rejected["reconcile_event_cursor"] == 0
        assert rejected["state"] != "completed" and rejected["response"] is None
        assert client.post("/add", headers=AUTH, json=body).status_code == 502
        reconcile(client)
        still_rejected = row_at(ledger_path)
        assert still_rejected["state"] != "completed" and still_rejected["response"] is None
        assert json.loads(still_rejected["confirmed_event_ids"]) == []
        assert backend.count("POST") == 0

        backend.events = {
            "event-1": event("SUCCEEDED", session=body["session_id"]),
            "event-2": event("SUCCEEDED", session=body["session_id"]),
        }
        reconcile(client)
        completed = assert_completed(row_at(ledger_path), body)
        assert set(json.loads(row_at(ledger_path)["confirmed_event_ids"])) == {"event-1", "event-2"}
        cached = client.post("/add", headers=AUTH, json=body)
        assert cached.status_code == 200 and cached.json() == completed
    assert backend.count("POST") == 0
