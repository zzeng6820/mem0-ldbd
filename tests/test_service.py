"""Contract tests for the recovered leaderboard adapter; no live API calls."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


AUTH = {"X-Api-Key": "wrapper-test-key"}
ADD_PATH = "/v1/memories/"
SEARCH_PATH = "/v2/memories/search/"
EVENT_PATH = "/v1/event/event-1/"
MEMORIES = [{"id": "memory-1", "memory": "The user likes tea.", "event": "ADD"}]
DONE = {"status": "SUCCEEDED", "results": MEMORIES}


class Upstream:
    """Record outbound requests and emulate synchronous or asynchronous Mem0."""

    def __init__(self):
        self.calls = []
        self.add_path = ADD_PATH
        self.search_path = SEARCH_PATH
        self.add_response = {"results": MEMORIES}
        self.search_response = {
            "results": [
                {"id": "m1", "memory": "Tea is preferred.", "score": 0.9,
                 "created_at": "2026-08-01T08:00:00Z"},
                {"id": "m2", "memory": "Coffee is also acceptable.", "score": 0.8},
                {"id": "m3", "memory": "Milk is optional.", "score": 0.7},
            ]
        }
        self.events = deque([DONE])
        self.add_error = None
        self.add_status = 200

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers.get("authorization") == "Token mem0-test-key"
        if request.method == "POST" and request.url.path == self.add_path:
            if self.add_error:
                raise self.add_error("simulated uncertain upstream write", request=request)
            return httpx.Response(self.add_status, json=self.add_response)
        if request.method == "POST" and request.url.path == self.search_path:
            return httpx.Response(200, json=self.search_response)
        if request.method == "GET" and request.url.path == EVENT_PATH:
            event = self.events[0]
            if len(self.events) > 1:
                self.events.popleft()
            return httpx.Response(200, json=event)
        raise AssertionError(f"Unexpected upstream endpoint: {request.method} {request.url}")

    def count(self, method, path):
        return sum(call[:2] == (method, path) for call in self.calls)

    def bodies(self, method, path):
        return [body for verb, url, body in self.calls if (verb, url) == (method, path)]


@pytest.fixture
def payload():
    return {
        "request_id": "request-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "messages": [
            {"role": "user", "content": "I prefer tea.",
             "timestamp": 1704067200000},
        ],
    }


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def make_client(tmp_path):
    @contextmanager
    def build(backend, observe_http=None, **overrides):
        options = {
            "wrapper_api_key": "wrapper-test-key",
            "mem0_api_key": "mem0-test-key",
            "mem0_base_url": "https://mem0.test",
            "mem0_api_mode": "legacy",
            "db_path": str(tmp_path / "requests.sqlite3"),
            "upstream_timeout_seconds": 1,
            "poll_interval_seconds": 0.002,
            "add_wait_seconds": 1,
            "max_add_concurrency": 64,
            "max_search_concurrency": 64,
        }
        options.update(overrides)
        app = create_app(Settings(**options), transport=httpx.MockTransport(backend))
        original_app = app

        async def observed_app(scope, receive, send):
            if scope["type"] == "http":
                observe_http(scope)
            await original_app(scope, receive, send)

        with TestClient(observed_app if observe_http else app) as client:
            yield client

    return build


def assert_add_success(response, payload):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    for field in ("request_id", "user_id", "session_id"):
        assert body[field] == payload[field]


def assert_not_success(response):
    assert response.status_code >= 400, response.text
    assert response.json().get("success") is not True


def stored_request(tmp_path, payload):
    """Inspect the on-disk result independently of the production store API."""
    with sqlite3.connect(tmp_path / "requests.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM requests WHERE user_id = ? AND request_id = ?",
            (payload["user_id"], payload["request_id"]),
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize("headers", [{}, {"X-Api-Key": "wrong-key"}])
@pytest.mark.parametrize("path", ["/add", "/search"])
def test_requires_wrapper_authentication(make_client, upstream, payload, headers, path):
    request = payload if path == "/add" else {"query": "drink", "user_id": "user-1", "top_k": 2}
    with make_client(upstream) as client:
        response = client.post(path, json=request, headers=headers)
    assert response.status_code == 401
    assert upstream.calls == []


def test_add_preserves_ids_and_uses_official_api(make_client, upstream, payload):
    with make_client(upstream) as client:
        response = client.post("/add", json=payload, headers=AUTH)
    assert_add_success(response, payload)
    assert upstream.count("POST", ADD_PATH) == 1
    body = upstream.bodies("POST", ADD_PATH)[0]
    assert body["user_id"] == payload["user_id"]
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "I prefer tea."
    assert body["metadata"]["ldbd_message_timestamps"] == [1704067200000]


def test_duplicate_add_does_not_write_twice(make_client, upstream, payload):
    with make_client(upstream) as client:
        first = client.post("/add", json=payload, headers=AUTH)
        second = client.post("/add", json=payload, headers=AUTH)
    assert_add_success(first, payload)
    assert_add_success(second, payload)
    assert upstream.count("POST", ADD_PATH) == 1


def test_completed_request_survives_service_restart(make_client, upstream, payload):
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    assert upstream.count("POST", ADD_PATH) == 1


@pytest.mark.parametrize("changed_field", ["content", "session_id"])
def test_same_user_request_with_different_payload_conflicts(
    make_client, upstream, payload, changed_field
):
    different = copy.deepcopy(payload)
    if changed_field == "content":
        different["messages"][0]["content"] = "I prefer coffee."
    else:
        different["session_id"] = "session-2"
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
        conflict = client.post("/add", json=different, headers=AUTH)
    assert conflict.status_code == 409
    assert upstream.count("POST", ADD_PATH) == 1


def test_request_id_is_scoped_to_user(make_client, upstream, payload):
    other_user = copy.deepcopy(payload)
    other_user["user_id"] = "user-2"
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
        assert_add_success(client.post("/add", json=other_user, headers=AUTH), other_user)
    assert upstream.count("POST", ADD_PATH) == 2
    assert [item["user_id"] for item in upstream.bodies("POST", ADD_PATH)] == ["user-1", "user-2"]


def _contains_user_filter(filters, user_id):
    if isinstance(filters, dict):
        if filters.get("user_id") == user_id:
            return True
        return any(_contains_user_filter(value, user_id) for value in filters.values())
    if isinstance(filters, list):
        return any(_contains_user_filter(value, user_id) for value in filters)
    return False


def test_search_is_user_scoped_normalizes_memory_and_limits_top_k(make_client, upstream):
    with make_client(upstream) as client:
        response = client.post("/search", headers=AUTH, json={
            "query": "What does the user drink?", "user_id": "user-1", "top_k": 2,
        })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data) == 2
    assert [(row["id"], row["content"]) for row in data] == [
        ("m1", "Tea is preferred."), ("m2", "Coffee is also acceptable."),
    ]
    body = upstream.bodies("POST", SEARCH_PATH)[0]
    assert body["query"] == "What does the user drink?"
    assert body["top_k"] == 2
    assert _contains_user_filter(body["filters"], "user-1"), body
    assert "session_id" not in json.dumps(body["filters"])
    assert "run_id" not in json.dumps(body["filters"])


def test_explicit_synchronous_add_is_supported(make_client, upstream, payload):
    payload["async_mode"] = False
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)


def test_async_add_cannot_claim_synchronous_success(make_client, upstream, payload):
    payload["async_mode"] = True
    with make_client(upstream) as client:
        response = client.post("/add", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert upstream.calls == []


def test_add_waits_for_pending_event_completion(make_client, upstream, payload):
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    upstream.events = deque([{"status": "PENDING"}, DONE])
    with make_client(upstream) as client:
        response = client.post("/add", json=payload, headers=AUTH)
    assert_add_success(response, payload)
    assert upstream.count("POST", ADD_PATH) == 1
    assert upstream.count("GET", EVENT_PATH) == 2


@pytest.mark.parametrize("status", ["SUCCESS", "COMPLETED"])
def test_legacy_event_success_statuses_are_supported(make_client, upstream, payload, status):
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    upstream.events = deque([{"status": status, "results": MEMORIES}])
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    assert upstream.count("POST", ADD_PATH) == 1


def test_failed_event_never_reports_add_success(make_client, upstream, payload):
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    upstream.events = deque([{"status": "FAILED", "error": "extraction failed"}])
    with make_client(upstream) as client:
        first = client.post("/add", json=payload, headers=AUTH)
        repeat = client.post("/add", json=payload, headers=AUTH)
    assert_not_success(first)
    assert_not_success(repeat)
    assert upstream.count("POST", ADD_PATH) == 1


def test_pending_event_timeout_resumes_polling_without_readding(make_client, upstream, payload):
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    upstream.events = deque([{"status": "PENDING"}])
    with make_client(upstream, add_wait_seconds=0.015) as client:
        pending = client.post("/add", json=payload, headers=AUTH)
        assert_not_success(pending)
        polls_before = upstream.count("GET", EVENT_PATH)
        assert polls_before >= 1
        upstream.events = deque([DONE])
        completed = client.post("/add", json=payload, headers=AUTH)
    assert_add_success(completed, payload)
    assert upstream.count("POST", ADD_PATH) == 1
    assert upstream.count("GET", EVENT_PATH) > polls_before


def test_pending_event_can_resume_after_restart(make_client, upstream, payload):
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    upstream.events = deque([{"status": "PENDING"}])
    with make_client(upstream, add_wait_seconds=0.015) as client:
        assert_not_success(client.post("/add", json=payload, headers=AUTH))
    upstream.events = deque([DONE])
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    assert upstream.count("POST", ADD_PATH) == 1


def test_uncertain_write_timeout_never_resubmits_even_after_restart(make_client, upstream, payload):
    upstream.add_error = httpx.ReadTimeout
    with make_client(upstream) as client:
        first = client.post("/add", json=payload, headers=AUTH)
        repeat = client.post("/add", json=payload, headers=AUTH)
    upstream.add_error = None
    with make_client(upstream) as client:
        after_restart = client.post("/add", json=payload, headers=AUTH)
    for response in (first, repeat, after_restart):
        assert_not_success(response)
    assert upstream.count("POST", ADD_PATH) == 1


@pytest.mark.parametrize("unknown", [{}, {"message": "accepted"}, {"results": "unexpected"}])
def test_unknown_add_response_cannot_be_mistaken_for_success(make_client, upstream, payload, unknown):
    upstream.add_response = unknown
    with make_client(upstream) as client:
        assert_not_success(client.post("/add", json=payload, headers=AUTH))
        assert_not_success(client.post("/add", json=payload, headers=AUTH))
    assert upstream.count("POST", ADD_PATH) == 1


def test_unknown_event_status_cannot_be_mistaken_for_success(make_client, upstream, payload):
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    upstream.events = deque([{"status": "SOMETHING_NEW"}])
    with make_client(upstream, add_wait_seconds=0.015) as client:
        assert_not_success(client.post("/add", json=payload, headers=AUTH))
    assert upstream.count("POST", ADD_PATH) == 1


def test_explicit_rate_limit_rejection_can_be_retried(make_client, upstream, payload):
    upstream.add_status = 429
    with make_client(upstream) as client:
        rejected = client.post("/add", json=payload, headers=AUTH)
        assert rejected.status_code == 429
        upstream.add_status = 200
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    assert upstream.count("POST", ADD_PATH) == 2


def test_v3_mode_uses_v3_add_and_search_endpoints(make_client, upstream, payload):
    upstream.add_path = "/v3/memories/add/"
    upstream.search_path = "/v3/memories/search/"
    upstream.add_response = {"event_id": "event-1", "status": "PENDING"}
    with make_client(upstream, mem0_api_mode="v3") as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
        found = client.post("/search", headers=AUTH, json={
            "query": "drink", "user_id": "user-1", "top_k": 1,
        })
    assert found.status_code == 200
    assert found.json()["data"][0]["content"] == "Tea is preferred."
    assert upstream.count("POST", upstream.add_path) == 1
    assert upstream.count("POST", upstream.search_path) == 1
    assert upstream.count("GET", EVENT_PATH) == 1


@pytest.mark.parametrize("user_id", ["*", "user-*", "*user"])
@pytest.mark.parametrize("path", ["/add", "/search"])
def test_wildcard_user_ids_are_rejected_before_upstream(
    make_client, upstream, payload, path, user_id
):
    request = copy.deepcopy(payload) if path == "/add" else {"query": "drink", "top_k": 2}
    request["user_id"] = user_id
    with make_client(upstream) as client:
        response = client.post(path, json=request, headers=AUTH)
    assert response.status_code == 422
    assert upstream.calls == []


@pytest.mark.parametrize("upstream_status", [401, 403])
def test_rejected_mem0_credentials_report_service_unavailable_and_allow_retry(
    make_client, upstream, payload, tmp_path, upstream_status
):
    upstream.add_status = upstream_status
    with make_client(upstream) as client:
        rejected = client.post("/add", json=payload, headers=AUTH)
    assert rejected.status_code == 503
    assert_not_success(rejected)
    assert stored_request(tmp_path, payload)["state"] == "retryable"
    upstream.add_status = 200
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    assert upstream.count("POST", ADD_PATH) == 2
    assert stored_request(tmp_path, payload)["state"] == "completed"


def test_upstream_conflict_is_uncertain_and_never_leaks_skip_compatible_409(
    make_client, upstream, payload, tmp_path
):
    upstream.add_status = 409
    upstream.add_response = {"detail": "request_id is still processing"}
    with make_client(upstream) as client:
        first = client.post("/add", json=payload, headers=AUTH)
        repeat = client.post("/add", json=payload, headers=AUTH)
    assert first.status_code == 502
    assert repeat.status_code == 503
    assert stored_request(tmp_path, payload)["state"] == "uncertain"
    upstream.add_status = 200
    upstream.add_response = {"results": MEMORIES}
    with make_client(upstream) as client:
        after_restart = client.post("/add", json=payload, headers=AUTH)
    assert after_restart.status_code == 503
    assert upstream.count("POST", ADD_PATH) == 1
    assert stored_request(tmp_path, payload)["state"] == "uncertain"


def test_concurrent_identical_adds_share_one_write_and_wait_for_completion(
    make_client, upstream, payload, tmp_path
):
    count = 4
    entered = 0
    all_arrived = threading.Event()
    upstream_started = threading.Event()
    release_upstream = threading.Event()
    ingress_lock = threading.Lock()

    def observe_http(scope):
        nonlocal entered
        if scope["path"] == "/add":
            with ingress_lock:
                entered += 1
                if entered == count:
                    all_arrived.set()

    async def blocked_backend(request):
        response = upstream(request)
        if request.method == "POST" and request.url.path == ADD_PATH:
            upstream_started.set()
            released = await asyncio.to_thread(release_upstream.wait, 5)
            assert released, "Test did not release the simulated upstream write"
        return response

    with make_client(
        blocked_backend, observe_http=observe_http, upstream_timeout_seconds=10
    ) as client:
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(client.post, "/add", json=payload, headers=AUTH)
                       for _ in range(count)]
            try:
                assert upstream_started.wait(3), "No add reached the upstream"
                assert all_arrived.wait(3), "Concurrent requests did not enter the application"
                completed, _ = wait(futures, timeout=0.1)
                assert not completed, "A duplicate returned before the shared write completed"
                assert upstream.count("POST", ADD_PATH) == 1
                assert stored_request(tmp_path, payload)["state"] == "submitted"
            finally:
                release_upstream.set()
            responses = [future.result(timeout=3) for future in futures]
    for response in responses:
        assert_add_success(response, payload)
    assert upstream.count("POST", ADD_PATH) == 1
    row = stored_request(tmp_path, payload)
    assert row["state"] == "completed"
    assert json.loads(row["response"])["success"] is True
    with make_client(upstream) as client:
        assert_add_success(client.post("/add", json=payload, headers=AUTH), payload)
    assert upstream.count("POST", ADD_PATH) == 1
