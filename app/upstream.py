"""Explicit Mem0 Platform REST adapter; never replay an uncertain add request."""

from __future__ import annotations

import asyncio
import math
from typing import Any
from urllib.parse import quote

import httpx


class UpstreamError(Exception):
    """Safe public error: never contains upstream bodies, URLs, or credentials."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retryable = retryable
        self.ambiguous = ambiguous


_SUCCESS = frozenset({"SUCCEEDED", "SUCCESS", "COMPLETED"})
_PENDING = frozenset({"PENDING", "RUNNING", "QUEUED", "PROCESSING", "IN_PROGRESS"})
_FAILED = frozenset({"FAILED", "FAILURE", "ERROR", "CANCELED", "CANCELLED", "REJECTED"})
_RESULT_KEYS = ("results", "memories", "data")
_MUTATIONS = frozenset({"ADD", "UPDATE", "DELETE", "NONE", "NOOP", "NO_OP"})


def _status(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


class Mem0Upstream:
    def __init__(self, settings: Any, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.mode = getattr(settings, "mem0_api_mode", "legacy")
        if self.mode not in {"legacy", "v3"}:
            raise ValueError("mem0_api_mode must be legacy or v3")
        self.base_url = settings.mem0_base_url.rstrip("/")

    @staticmethod
    def _protocol_error(*, add: bool = False) -> UpstreamError:
        return UpstreamError(502, "Mem0 returned an unsupported response format", ambiguous=add)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        add: bool = False,
        deadline: float | None = None,
    ) -> Any:
        attempts = 1 if add else 3
        for attempt in range(attempts):
            timeout = float(self.settings.upstream_timeout_seconds)
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise UpstreamError(504, "Mem0 event wait timed out", retryable=True)
                timeout = min(timeout, remaining)
            retry_delay = min(0.5 * (2**attempt), 2.0)
            try:
                response = await asyncio.wait_for(
                    self.client.request(
                        method,
                        self.base_url + path,
                        json=body,
                        headers={
                            "Authorization": f"Token {self.settings.mem0_api_key}",
                            "Accept": "application/json",
                        },
                        timeout=timeout,
                        follow_redirects=False,
                    ),
                    timeout=timeout,
                )
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                timed_out = isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError))
                error = UpstreamError(
                    504 if timed_out else 502,
                    "Mem0 request timed out" if timed_out else "Mem0 transport request failed",
                    retryable=not add,
                    ambiguous=add,
                )
            else:
                code = response.status_code
                if not 200 <= code < 300:
                    retryable = code == 429 or (not add and (code == 408 or code >= 500))
                    if add and code in {401, 403}:
                        raise UpstreamError(503, "Mem0 credential was rejected", retryable=True)
                    error = UpstreamError(
                        (502 if add and code == 409 else code) if 400 <= code <= 599 else 502,
                        f"Mem0 request failed (HTTP {code})",
                        retryable=retryable,
                        ambiguous=add and (code in {408, 409} or code >= 500),
                    )
                    try:
                        delay = float(response.headers.get("Retry-After", ""))
                        if math.isfinite(delay):
                            retry_delay = min(max(delay, 0.0), 5.0)
                    except ValueError:
                        pass
                else:
                    try:
                        return response.json()
                    except (ValueError, UnicodeError):
                        raise self._protocol_error(add=add) from None
            if not error.retryable or attempt == attempts - 1:
                raise error from None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise UpstreamError(504, "Mem0 event wait timed out", retryable=True)
                retry_delay = min(retry_delay, remaining)
            await asyncio.sleep(retry_delay)
        raise AssertionError("unreachable")

    async def submit_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload["messages"]
        body: dict[str, Any] = {
            "messages": [{"role": item["role"], "content": item["content"]} for item in messages],
            "user_id": payload["user_id"],
            "run_id": payload["session_id"],
            "metadata": {
                "request_id": payload["request_id"],
                "ldbd_message_timestamps": [item.get("timestamp") for item in messages],
            },
        }
        if self.mode == "legacy":
            path = "/v1/memories/"
            body.update(async_mode=False, version="v2", output_format="v1.1")
        else:
            path = "/v3/memories/add/"
        response = await self._request("POST", path, body=body, add=True)
        pending = self._classify_add(response)
        if pending:
            return {"event_ids": list(dict.fromkeys(pending))}
        return {"completed": True}

    def _classify_add(self, value: Any) -> list[str]:
        """Accept known synchronous results or tracked events, never a generic 2xx."""
        if isinstance(value, list):
            event_ids: list[str] = []
            for item in value:
                if not isinstance(item, dict):
                    raise self._protocol_error(add=True)
                event_ids.extend(self._classify_add(item))
            return event_ids  # An empty list is a completed extraction with no facts.
        if not isinstance(value, dict):
            raise self._protocol_error(add=True)
        status = _status(value.get("status"))
        if status in _FAILED or value.get("success") is False or value.get("error"):
            raise UpstreamError(502, "Mem0 memory extraction failed")
        event_id = value.get("event_id")
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id.strip():
                raise self._protocol_error(add=True)
            if status in _SUCCESS:
                return []
            if not status or status in _PENDING:
                return [event_id]
            raise self._protocol_error(add=True)
        if status in _PENDING:
            raise self._protocol_error(add=True)  # Cannot safely track this write.
        if status and status not in _SUCCESS:
            raise self._protocol_error(add=True)
        for key in _RESULT_KEYS:
            if key in value and isinstance(value[key], list):
                return self._classify_add(value[key])
        # Historical sync add output: {id, event: ADD|UPDATE|DELETE, data: {...}}.
        if isinstance(value.get("id"), str) and value["id"]:
            mutation = _status(value.get("event"))
            if mutation in _MUTATIONS:
                return []
            if isinstance(value.get("memory"), str):
                return []
        raise self._protocol_error(add=True)

    async def wait_events(self, event_ids: list[str]) -> None:
        pending = list(dict.fromkeys(event_ids))
        if any(not isinstance(item, str) or not item.strip() for item in pending):
            raise self._protocol_error()
        deadline = asyncio.get_running_loop().time() + float(self.settings.add_wait_seconds)
        while pending:
            remaining_events: list[str] = []
            for event_id in pending:
                response = await self._request(
                    "GET", f"/v1/event/{quote(event_id, safe='')}/", deadline=deadline
                )
                if not isinstance(response, dict):
                    raise self._protocol_error()
                status = _status(response.get("status"))
                if status in _FAILED or response.get("success") is False or response.get("error"):
                    raise UpstreamError(502, "Mem0 memory extraction failed")
                if status in _SUCCESS:
                    continue
                if status not in _PENDING:
                    raise self._protocol_error()
                remaining_events.append(event_id)
            pending = remaining_events
            if pending:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise UpstreamError(504, "Mem0 event wait timed out", retryable=True)
                await asyncio.sleep(min(max(float(self.settings.poll_interval_seconds), 0.01), remaining))

    async def search(
        self, query: str, user_id: str, top_k: int, options: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not isinstance(user_id, str) or not user_id.strip() or "*" in user_id:
            raise UpstreamError(400, "user_id must be nonempty and cannot contain wildcards")
        if not isinstance(query, str) or not query.strip():
            raise UpstreamError(400, "query must not be empty")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise UpstreamError(400, "top_k must be a positive integer")
        body: dict[str, Any] = {"query": query, "filters": {"user_id": user_id}, "top_k": top_k}
        if "threshold" in options:
            threshold = options["threshold"]
            if (
                isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold)
                or not 0 <= threshold <= 1
            ):
                raise UpstreamError(400, "threshold must be between 0 and 1")
            body["threshold"] = threshold
        if "rerank" in options:
            if not isinstance(options["rerank"], bool):
                raise UpstreamError(400, "rerank must be a boolean")
            body["rerank"] = options["rerank"]
        path = "/v3/memories/search/" if self.mode == "v3" else "/v2/memories/search/"
        response = await self._request("POST", path, body=body)
        return self._normalize_search(response, top_k)

    def _normalize_search(self, response: Any, top_k: int) -> list[dict[str, Any]]:
        if isinstance(response, dict):
            if response.get("error") or response.get("success") is False:
                raise UpstreamError(502, "Mem0 search failed")
            for key in _RESULT_KEYS:
                if key in response:
                    response = response[key]
                    break
        if not isinstance(response, list):
            raise self._protocol_error()
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in response:
            if not isinstance(item, dict):
                raise self._protocol_error()
            memory_id = item.get("id")
            content = item.get("memory", item.get("content"))
            if (not isinstance(memory_id, str) or not memory_id.strip()
                    or not isinstance(content, str) or not content.strip()):
                raise self._protocol_error()
            normalized: dict[str, Any] = {"id": memory_id, "content": content}
            if item.get("score") is not None:
                score = item["score"]
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise self._protocol_error()
                normalized["score"] = score
            if item.get("created_at") is not None:
                if not isinstance(item["created_at"], str):
                    raise self._protocol_error()
                normalized["created_at"] = item["created_at"]
            if memory_id not in seen:
                seen.add(memory_id)
                results.append(normalized)
        return results[:top_k]
