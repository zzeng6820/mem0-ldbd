import asyncio
import copy
import hashlib
import json
import sqlite3

import httpx
import pytest

from app.partitioning import split_messages, part_request_id
from app.store import RequestStore
from test_service import AUTH, ADD_PATH, DONE, MEMORIES, make_client, upstream, payload


def large(payload):
    result = copy.deepcopy(payload)
    result["messages"] = [{"role": "user" if i % 2 == 0 else "assistant",
                           "content": f"message {i}", "timestamp": 1704067200000 + i}
                          for i in range(45)]
    return result


def fingerprint(body):
    return hashlib.sha256(json.dumps(
        {"payload": body, "upstream": "https://mem0.test", "mode": "legacy"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_message_and_character_limits_preserve_every_character_and_timestamp():
    messages = [{"role": "assistant", "content": "  code\n" * 5000, "timestamp": 13},
                {"role": "user", "content": "next", "timestamp": 14}]
    parts = split_messages(messages)
    assert all(len(part) <= 20 and sum(len(m["content"]) for m in part) <= 20000 for part in parts)
    flattened = [m for part in parts for m in part]
    assert "".join(m["content"] for m in flattened[:-1]) == messages[0]["content"]
    assert all(m["timestamp"] == 13 and m["role"] == "assistant" for m in flattened[:-1])
    assert flattened[-1] == messages[1]
    assert part_request_id("u", "r", "f", 0) == part_request_id("u", "r", "f", 0)
    assert part_request_id("u", "r", "f", 0) != part_request_id("u", "r", "f", 1)


def test_large_add_is_partitioned_once_and_cached_across_restart(make_client, upstream, payload):
    body = large(payload)
    with make_client(upstream) as client:
        first = client.post("/add", headers=AUTH, json=body)
        assert first.status_code == 200
        assert first.json()["request_id"] == body["request_id"]
        assert client.post("/add", headers=AUTH, json=body).json() == first.json()
    with make_client(upstream) as client:
        assert client.post("/add", headers=AUTH, json=body).json() == first.json()
    assert upstream.count("POST", ADD_PATH) == 3
    sent = [m for part in upstream.bodies("POST", ADD_PATH) for m in part["messages"]]
    assert sent == [{"role": m["role"], "content": m["content"]} for m in body["messages"]]
    timestamps = [t for part in upstream.bodies("POST", ADD_PATH)
                  for t in part["metadata"]["ldbd_message_timestamps"]]
    assert timestamps == [m["timestamp"] for m in body["messages"]]


@pytest.mark.parametrize("failure", ["timeout", "400"])
def test_partial_progress_never_resends_completed_or_uncertain_parts(
    make_client, upstream, payload, failure
):
    body = large(payload)
    def backend(request):
        result = upstream(request)
        if request.method == "POST" and upstream.count("POST", ADD_PATH) == 2:
            if failure == "timeout":
                raise httpx.ReadTimeout("outcome unknown", request=request)
            return httpx.Response(400, json={"error": "rejected"})
        return result
    with make_client(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
    with make_client(backend) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
    assert upstream.count("POST", ADD_PATH) == 2


@pytest.mark.parametrize("state,status", [("failed", 400), ("uncertain", 504), ("submitted", None)])
def test_old_untracked_writes_are_not_automatically_partitioned(
    make_client, upstream, payload, tmp_path, state, status
):
    body = large(payload)
    store = RequestStore(str(tmp_path / "requests.sqlite3"))
    store.claim(body["user_id"], body["request_id"], fingerprint(body))
    store.update(body["user_id"], body["request_id"], state, status_code=status)
    with make_client(upstream) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code >= 400
    assert upstream.calls == []


def test_explicit_repair_ready_can_partition_rejected_request(make_client, upstream, payload, tmp_path):
    body = large(payload)
    store = RequestStore(str(tmp_path / "requests.sqlite3"))
    store.claim(body["user_id"], body["request_id"], fingerprint(body))
    store.update(body["user_id"], body["request_id"], "repair_ready", status_code=400)
    with make_client(upstream) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code == 200
    assert upstream.count("POST", ADD_PATH) == 3
    row = store.get(body["user_id"], body["request_id"])
    assert row["state"] == "completed" and row["status_code"] is None


def test_existing_event_uses_get_instead_of_partitioned_writes(make_client, upstream, payload, tmp_path):
    body = large(payload)
    store = RequestStore(str(tmp_path / "requests.sqlite3"))
    store.claim(body["user_id"], body["request_id"], fingerprint(body))
    store.update(body["user_id"], body["request_id"], "pending", event_ids=["event-1"])
    with make_client(upstream) as client:
        assert client.post("/add", headers=AUTH, json=body).status_code == 200
    assert upstream.count("POST", ADD_PATH) == 0


def test_restart_reconciles_parent_only_after_all_children_exist_and_complete(
    make_client, upstream, payload, tmp_path, monkeypatch
):
    body = large(payload)
    real_complete = RequestStore.complete
    def crash_parent(store, user, request, response, **kwargs):
        if request == body["request_id"]:
            raise RuntimeError("crash after all child receipts")
        return real_complete(store, user, request, response, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(RequestStore, "complete", crash_parent)
        with make_client(upstream) as client:
            with pytest.raises(RuntimeError, match="crash after all child receipts"):
                client.post("/add", headers=AUTH, json=body)
    with make_client(upstream) as client:
        summary = client.portal.call(client.app.state.reconciler.reconcile_once)
        assert summary["completed"] == 1
        assert client.post("/add", headers=AUTH, json=body).status_code == 200
    assert upstream.count("POST", ADD_PATH) == 3
    store = RequestStore(str(tmp_path / "requests.sqlite3"))
    other = {**body, "request_id": "missing-child-parent"}
    receipt = {"success": True, **{k: other[k] for k in ("user_id", "request_id", "session_id")}}
    store.claim(other["user_id"], other["request_id"], "other-fingerprint", receipt)
    store.set_part_requests(other["user_id"], other["request_id"], ["not-submitted"])
    with make_client(upstream) as client:
        client.portal.call(client.app.state.reconciler.reconcile_once)
    assert store.get(other["user_id"], other["request_id"])["state"] != "completed"
    assert upstream.count("POST", ADD_PATH) == 3
