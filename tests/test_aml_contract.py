"""AML input contract: retain text, skip images, and never invoke image services."""
from __future__ import annotations

import base64
import copy
import json
import socket
import sqlite3
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import RequestStore


AUTH = {"X-Api-Key": "aml-wrapper-test-key"}
MIB = 1024 * 1024
ADD_PATH = "/v1/memories/"
SEARCH_PATH = "/v2/memories/search/"


def text_part(value):
    return {"type": "text", "text": value}


def image_part(data=b"synthetic image bytes", mime="png"):
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{encoded}"}}


def request_body(path, content):
    if path == "/add":
        return {
            "request_id": "aml-request-1", "user_id": "aml-user-1", "session_id": "aml-session-1",
            "messages": [{"role": "user", "content": content, "timestamp": 1704067200000}],
        }
    return {"query": content, "user_id": "aml-user-1", "top_k": 2}


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    calls = []

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Text-only requests must not perform real network or image-service calls")

    async def forbidden_async_network(*args, **kwargs):
        forbidden_network()

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async_network)

    def backend(request):
        assert request.url.host == "mem0.test", "Unexpected image/provider request"
        assert request.method == "POST"
        assert request.url.path in {ADD_PATH, SEARCH_PATH}
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == ADD_PATH:
            return httpx.Response(200, json={"results": [{
                "id": "aml-memory-1", "memory": "The user likes tea.", "event": "ADD",
            }]})
        return httpx.Response(200, json={"results": [{
            "id": "aml-memory-1", "memory": "The user likes tea.", "score": 0.9,
        }]})

    settings = Settings(
        wrapper_api_key="aml-wrapper-test-key", mem0_api_key="aml-mem0-test-key",
        mem0_base_url="https://mem0.test", mem0_api_mode="legacy",
        db_path=str(tmp_path / "aml-requests.sqlite3"),
        upstream_timeout_seconds=1, poll_interval_seconds=0.001, add_wait_seconds=1,
    )
    @contextmanager
    def build():
        with TestClient(create_app(settings, transport=httpx.MockTransport(backend))) as client:
            yield client, calls

    return build


@pytest.fixture
def service(service_factory):
    with service_factory() as opened:
        yield opened


def assert_success(response, ignored_images=0):
    assert response.status_code == 200, response.text[:500]
    body = response.json()
    assert body["success"] is True
    if ignored_images:
        assert body["ignored_image_parts"] == ignored_images
        assert isinstance(body["warnings"], list)
        assert body["warnings"] and all(isinstance(warning, str) and warning for warning in body["warnings"])


def assert_private_error(response, expected_status, sensitive_values=()):
    assert response.status_code == expected_status, response.text[:300]

    def check_keys(value):
        if isinstance(value, dict):
            assert "input" not in value, "Validation errors must not expose the original input"
            assert "ctx" not in value, "Validation errors must not expose validation context"
            for item in value.values():
                check_keys(item)
        elif isinstance(value, list):
            for item in value:
                check_keys(item)

    check_keys(response.json())
    for sensitive in sensitive_values:
        leaked = sensitive in response.text
        assert not leaked, "Validation error echoed image data"


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_string_text_is_preserved_exactly(service, path):
    client, calls = service
    original = "  第一行：tea\nsecond line\t  "
    response = client.post(path, headers=AUTH, json=request_body(path, original))
    assert_success(response)
    assert len(calls) == 1
    outgoing = calls[0][1]
    actual = outgoing["messages"][0]["content"] if path == "/add" else outgoing["query"]
    assert actual == original


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_text_parts_keep_order_and_whitespace_with_newline_separators(service, path):
    client, calls = service
    parts = [text_part(" first  "), text_part("第二段\n续行"), text_part(" final ")]
    response = client.post(path, headers=AUTH, json=request_body(path, parts))
    assert_success(response)
    actual = calls[0][1]["messages"][0]["content"] if path == "/add" else calls[0][1]["query"]
    assert actual == " first  \n第二段\n续行\n final "


@pytest.mark.parametrize("path", ["/add", "/search"])
@pytest.mark.parametrize("mime", ["jpeg", "png", "webp"])
def test_mixed_parts_skip_images_and_report_count_without_forwarding_data(service, path, mime):
    client, calls = service
    picture = image_part(mime=mime)
    parts = [picture, text_part(" alpha "), picture, text_part("beta")]
    response = client.post(path, headers=AUTH, json=request_body(path, parts))
    assert_success(response, ignored_images=2)
    assert len(calls) == 1
    outgoing = calls[0][1]
    actual = outgoing["messages"][0]["content"] if path == "/add" else outgoing["query"]
    assert actual == " alpha \nbeta"
    serialized = json.dumps(outgoing)
    assert "image_url" not in serialized and "data:image/" not in serialized
    assert picture["image_url"]["url"].split(",", 1)[1] not in serialized


def test_image_only_messages_are_skipped_and_remaining_roles_and_timestamps_stay_aligned(service):
    client, calls = service
    body = request_body("/add", "First text")
    body["messages"] = [
        {"role": "user", "content": "First text", "timestamp": 1704067200000},
        {"role": "assistant", "content": [image_part(), image_part()], "timestamp": 1704067201000},
        {"role": "assistant", "content": [text_part("Second text"), image_part()],
         "timestamp": 1704067202000},
    ]
    response = client.post("/add", headers=AUTH, json=body)
    assert_success(response, ignored_images=3)
    assert calls[0][1]["messages"] == [
        {"role": "user", "content": "First text"},
        {"role": "assistant", "content": "Second text"},
    ]
    assert calls[0][1]["metadata"]["ldbd_message_timestamps"] == [1704067200000, 1704067202000]


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_image_only_request_succeeds_without_calling_upstream(service, path):
    client, calls = service
    picture = image_part()
    body = request_body(path, [picture, picture])
    response = client.post(path, headers=AUTH, json=body)
    assert_success(response, ignored_images=2)
    result = response.json()
    if path == "/add":
        assert result["no_op"] is True
        for key in ("request_id", "user_id", "session_id"):
            assert result[key] == body[key]
    else:
        assert result["data"] == []
    assert calls == []


def test_image_only_add_receipt_is_persisted_and_replayed_after_restart(service_factory, tmp_path):
    body = request_body("/add", [image_part(b"first picture"), image_part(b"second picture")])
    with service_factory() as (client, calls):
        first = client.post("/add", headers=AUTH, json=body)
        assert_success(first, ignored_images=2)
        repeat = client.post("/add", headers=AUTH, json=body)
        assert_success(repeat, ignored_images=2)
        assert repeat.json() == first.json()
        assert calls == []
    with sqlite3.connect(tmp_path / "aml-requests.sqlite3") as connection:
        row = connection.execute(
            "SELECT state, response FROM requests WHERE user_id = ? AND request_id = ?",
            (body["user_id"], body["request_id"]),
        ).fetchone()
    assert row is not None and row[0] == "completed"
    assert json.loads(row[1]) == first.json()
    with service_factory() as (client, calls):
        after_restart = client.post("/add", headers=AUTH, json=body)
        assert_success(after_restart, ignored_images=2)
        assert after_restart.json() == first.json()
        changed = copy.deepcopy(body)
        changed["messages"][0]["content"][0] = image_part(b"changed picture")
        assert client.post("/add", headers=AUTH, json=changed).status_code == 409
        assert calls == []


def test_image_only_add_recovers_after_crash_before_receipt_commit(
    service_factory, tmp_path, monkeypatch
):
    body = request_body("/add", [image_part(b"image before simulated crash")])
    real_update = RequestStore.update

    def interrupt_before_receipt(store, user_id, request_id, state, **fields):
        if state == "completed":
            raise RuntimeError("Simulated crash before no-op receipt commit")
        return real_update(store, user_id, request_id, state, **fields)

    with monkeypatch.context() as fault:
        fault.setattr(RequestStore, "update", interrupt_before_receipt)
        with service_factory() as (client, calls):
            with pytest.raises(RuntimeError, match="Simulated crash before no-op receipt commit"):
                client.post("/add", headers=AUTH, json=body)
            assert calls == []
    with sqlite3.connect(tmp_path / "aml-requests.sqlite3") as connection:
        row = connection.execute(
            "SELECT state, response FROM requests WHERE user_id = ? AND request_id = ?",
            (body["user_id"], body["request_id"]),
        ).fetchone()
    assert row == ("submitted", None)
    with service_factory() as (client, calls):
        recovered = client.post("/add", headers=AUTH, json=body)
        assert_success(recovered, ignored_images=1)
        assert recovered.json()["no_op"] is True
        for key in ("request_id", "user_id", "session_id"):
            assert recovered.json()[key] == body[key]
        assert client.post("/add", headers=AUTH, json=body).json() == recovered.json()
        assert calls == []
    with sqlite3.connect(tmp_path / "aml-requests.sqlite3") as connection:
        row = connection.execute(
            "SELECT state, response FROM requests WHERE user_id = ? AND request_id = ?",
            (body["user_id"], body["request_id"]),
        ).fetchone()
    assert row[0] == "completed" and json.loads(row[1]) == recovered.json()


@pytest.mark.parametrize("role", ["system", "tool", "developer"])
def test_add_rejects_roles_outside_user_and_assistant(service, role):
    client, calls = service
    body = request_body("/add", "Text")
    body["messages"][0]["role"] = role
    assert_private_error(client.post("/add", headers=AUTH, json=body), 422)
    assert calls == []


@pytest.mark.parametrize("value", [None, True, "2", 1.5])
def test_search_requires_an_explicit_integer_top_k(service, value):
    client, calls = service
    body = request_body("/search", "Question")
    if value is None:
        body.pop("top_k")
    else:
        body["top_k"] = value
    assert_private_error(client.post("/search", headers=AUTH, json=body), 422)
    assert calls == []


@pytest.mark.parametrize("choices", [None, [], ["A. Tea", "B. Coffee"]])
def test_answer_choices_are_accepted_without_altering_query_or_mem0_options(service, choices):
    client, calls = service
    body = request_body("/search", [text_part("Question"), image_part(), text_part("Follow-up")])
    body["options"] = choices
    body["search_options"] = {"threshold": 0.3, "rerank": True}
    response = client.post("/search", headers=AUTH, json=body)
    assert_success(response, ignored_images=1)
    outgoing = calls[0][1]
    assert outgoing["query"] == "Question\nFollow-up"
    assert outgoing["threshold"] == 0.3 and outgoing["rerank"] is True
    assert "options" not in outgoing and "search_options" not in outgoing
    assert "A. Tea" not in json.dumps(outgoing)


@pytest.mark.parametrize("choices", [[""], ["  "], ["Tea", 7], "Tea", {"rerank": True}])
def test_answer_choices_require_nonblank_strings(service, choices):
    client, calls = service
    body = request_body("/search", "Question")
    body["options"] = choices
    assert_private_error(client.post("/search", headers=AUTH, json=body), 422)
    assert calls == []


@pytest.mark.parametrize("path", ["/add", "/search"])
@pytest.mark.parametrize("url", [
    "https://images.invalid/picture.png",
    "file:///private/image.png",
    "data:image/gif;base64,YWJj",
    "data:image/jpg;base64,YWJj",
    "data:image/svg+xml;base64,YWJj",
    "data:image/png;base64,@@invalid@@",
    "data:image/png;base64,YWJ",
    "data:image/png;base64,",
])
def test_invalid_or_external_images_are_rejected_even_when_text_is_present(service, path, url):
    client, calls = service
    parts = [text_part("Valid text"), {"type": "image_url", "image_url": {"url": url}}]
    response = client.post(path, headers=AUTH, json=request_body(path, parts))
    assert_private_error(response, 422, [url])
    assert calls == []


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_validation_error_does_not_echo_image_data_used_as_invalid_discriminator(service, path):
    client, calls = service
    picture = image_part(b"PRIVATE-IMAGE-SENTINEL-DO-NOT-ECHO")
    sensitive = picture["image_url"]["url"]
    picture["type"] = sensitive
    response = client.post(path, headers=AUTH, json=request_body(path, [picture]))
    assert_private_error(response, 422, [sensitive, sensitive.split(",", 1)[1]])
    assert calls == []


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_validation_error_does_not_echo_private_unknown_field_names_in_locations(service, path):
    client, calls = service
    private_field = "SYNTHETIC-PRIVATE-FIELD-DO-NOT-ECHO-9f0375"
    body = request_body(path, "Valid text")
    body[private_field] = "Unexpected value"
    response = client.post(path, headers=AUTH, json=body)
    assert_private_error(response, 422, [private_field])
    assert calls == []


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_individual_image_at_ten_mib_limit_is_accepted_and_skipped(service, path):
    client, calls = service
    picture = image_part(b"x" * (10 * MIB))
    response = client.post(path, headers=AUTH, json=request_body(path, [text_part("Text"), picture]))
    assert_success(response, ignored_images=1)
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["/add", "/search"])
def test_individual_image_above_ten_mib_is_rejected_without_echoing_payload(service, path):
    client, calls = service
    picture = image_part(b"x" * (10 * MIB + 1))
    response = client.post(path, headers=AUTH, json=request_body(path, [text_part("Text"), picture]))
    assert_private_error(response, 422, [picture["image_url"]["url"].split(",", 1)[1][:100]])
    assert calls == []


def test_add_accepts_exactly_thirty_mib_across_images(service):
    client, calls = service
    picture = image_part(b"x" * (10 * MIB))
    response = client.post("/add", headers=AUTH, json=request_body(
        "/add", [text_part("Text"), picture, picture, picture],
    ))
    assert_success(response, ignored_images=3)
    assert len(calls) == 1


def test_add_rejects_over_thirty_mib_across_messages_including_skipped_messages(service):
    client, calls = service
    picture = image_part(b"x" * (8 * MIB))
    body = request_body("/add", [text_part("Text"), picture])
    body["messages"].extend({"role": "assistant", "content": [picture]} for _ in range(3))
    response = client.post("/add", headers=AUTH, json=body)
    assert_private_error(response, 413, [picture["image_url"]["url"].split(",", 1)[1][:100]])
    assert calls == []


def test_search_has_no_thirty_mib_total_limit_when_individual_images_are_valid(service):
    client, calls = service
    picture = image_part(b"x" * (8 * MIB))
    response = client.post("/search", headers=AUTH, json=request_body(
        "/search", [text_part("Question"), picture, picture, picture, picture],
    ))
    assert_success(response, ignored_images=4)
    assert calls[0][1]["query"] == "Question"
    assert len(calls) == 1


@pytest.mark.parametrize("with_text", [True, False])
def test_skipped_image_content_still_participates_in_add_idempotency(service, with_text):
    client, calls = service
    parts = [text_part("Same text")] if with_text else []
    parts.append(image_part(b"first image"))
    body = request_body("/add", parts)
    first = client.post("/add", headers=AUTH, json=body)
    repeat = client.post("/add", headers=AUTH, json=body)
    assert_success(first, ignored_images=1)
    assert repeat.json() == first.json()
    changed = copy.deepcopy(body)
    changed["messages"][0]["content"][-1] = image_part(b"different image")
    conflict = client.post("/add", headers=AUTH, json=changed)
    assert conflict.status_code == 409
    assert len(calls) == (1 if with_text else 0)
