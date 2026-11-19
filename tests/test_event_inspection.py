"""Read-only reconciliation must prove completion and the original request scope."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.upstream import Mem0Upstream, UpstreamError


RECEIPT = {"success": True, "user_id": "user-1", "request_id": "request-1", "session_id": "session-1"}
PAYLOAD = {"user_id": "user-1", "run_id": "session-1", "metadata": {"request_id": "request-1"}}


def inspect(events, *, receipt=RECEIPT, event_ids=None):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert not request.content
        response = events[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    async def run():
        settings = SimpleNamespace(
            mem0_base_url="https://mem0.test", mem0_api_key="hidden-test-key", upstream_timeout_seconds=1
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Mem0Upstream(settings, client)
            return await adapter.inspect_events(
                event_ids if event_ids is not None else [f"event-{i}" for i in range(len(events))],
                user_id="user-1", request_id="request-1", receipt=receipt,
            )

    return asyncio.run(run()), calls


def test_all_success_states_require_all_events_and_only_get():
    events = [{"status": status, "payload": deepcopy(PAYLOAD)} for status in ("SUCCEEDED", "SUCCESS", "COMPLETED")]
    result, calls = inspect(events, receipt=None)
    assert result == {"completed": True, "status": "completed", "session_id": "session-1"}
    assert len(calls) == 3


@pytest.mark.parametrize("error, completed, status", [(False, True, "completed"), (True, False, "failed")])
def test_success_event_accepts_boolean_error(error, completed, status):
    result, calls = inspect([{"status": "SUCCEEDED", "error": error}])
    assert result == {"completed": completed, "status": status, "session_id": "session-1"}
    assert len(calls) == 1


@pytest.mark.parametrize("event, expected", [
    ({"status": "RUNNING"}, "pending"),
    ({"status": "FAILED"}, "failed"),
    ({"status": "SUCCEEDED", "success": False}, "failed"),
    ({"status": "SUCCEEDED", "error": "private-message"}, "failed"),
    ({"status": "NEW_UNRECOGNIZED_STATE"}, "unknown"),
])
def test_partial_success_cannot_restore_receipt(event, expected):
    result, calls = inspect([{"status": "SUCCEEDED"}, event])
    assert result == {"completed": False, "status": expected, "session_id": "session-1"}
    assert len(calls) == 2


def test_saved_receipt_can_supply_missing_payload_and_event_ids_are_encoded():
    result, calls = inspect([{"status": "SUCCEEDED"}], event_ids=["event/one?x", "event/one?x"])
    assert result["completed"] is True
    assert len(calls) == 1
    assert calls[0].url.raw_path == b"/v1/event/event%2Fone%3Fx/"


@pytest.mark.parametrize("payload", [
    None, {}, {"user_id": "user-1", "run_id": "session-1"},
    {"user_id": "user-1", "metadata": {"request_id": "request-1"}},
    {"user_id": "user-1", "run_id": "", "metadata": {"request_id": "request-1"}},
])
def test_old_records_need_all_payload_identity_fields(payload):
    with pytest.raises(UpstreamError, match="identity could not be verified"):
        inspect([{"status": "SUCCEEDED", "payload": payload, "metadata": {"request_id": "request-1"}}], receipt=None)


@pytest.mark.parametrize("event", [
    {"status": "SUCCEEDED", "payload": {"user_id": "other-user"}},
    {"status": "SUCCEEDED", "payload": {"run_id": "other-session"}},
    {"status": "SUCCEEDED", "payload": {"metadata": {"request_id": "other-request"}}},
    {"status": "SUCCEEDED", "metadata": {"request_id": "other-request"}},
    {"status": "SUCCEEDED", "id": "wrong-event"},
])
def test_persisted_receipt_does_not_override_conflicting_event_identity(event):
    with pytest.raises(UpstreamError, match="identity could not be verified"):
        inspect([event])


def test_old_events_must_agree_on_session():
    other = deepcopy(PAYLOAD)
    other["run_id"] = "other-session"
    with pytest.raises(UpstreamError, match="identity could not be verified"):
        inspect([{"status": "SUCCEEDED", "payload": PAYLOAD}, {"status": "SUCCEEDED", "payload": other}], receipt=None)


@pytest.mark.parametrize("event", [
    None, [], {}, {"status": 1}, {"status": "SUCCEEDED", "payload": []},
    {"status": "SUCCEEDED", "error": {}},
])
def test_malformed_event_is_rejected(event):
    with pytest.raises(UpstreamError) as error:
        inspect([event])
    assert error.value.status_code == 502
    assert not error.value.ambiguous


def test_empty_event_list_is_never_completed_and_invalid_receipt_is_rejected():
    result, calls = inspect([], receipt=None)
    assert result == {"completed": False, "status": "unknown", "session_id": None}
    assert calls == []
    with pytest.raises(UpstreamError, match="identity could not be verified"):
        inspect([], receipt={**RECEIPT, "user_id": "wrong-user"})


def test_transport_errors_are_safe_and_never_write():
    with pytest.raises(UpstreamError) as error:
        inspect([httpx.ReadError("private body hidden-test-key")] * 3, event_ids=["event-0"])
    assert error.value.retryable is True
    assert error.value.ambiguous is False
    assert "private" not in error.value.detail
    assert "hidden-test-key" not in error.value.detail
