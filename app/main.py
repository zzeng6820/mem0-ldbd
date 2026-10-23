"""Reconstructed leaderboard facade for Mem0 Platform (not recovered original source)."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .config import Settings
from .store import RequestStore
from .upstream import Mem0Upstream, UpstreamError


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)
    timestamp: StrictInt | None = None


class AddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=1024)
    user_id: str = Field(min_length=1, max_length=1024)
    session_id: str = Field(min_length=1, max_length=1024)
    messages: list[Message] = Field(min_length=1)
    async_mode: Literal[False] = False

    @field_validator("request_id", "user_id", "session_id")
    @classmethod
    def nonblank(cls, value, info):
        if not value.strip():
            raise ValueError("ID must not be blank")
        if info.field_name == "user_id" and "*" in value:
            raise ValueError("Wildcard user IDs are not allowed")
        return value


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: float | None = Field(default=None, ge=0, le=1)
    rerank: bool | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    user_id: str = Field(min_length=1, max_length=1024)
    top_k: int = Field(default=100, ge=1, le=1000)
    search_options: SearchOptions | None = None

    @field_validator("query", "user_id")
    @classmethod
    def nonblank(cls, value, info):
        if not value.strip():
            raise ValueError("Value must not be blank")
        if info.field_name == "user_id" and "*" in value:
            raise ValueError("Wildcard user IDs are not allowed")
        return value


def create_app(settings: Settings | None = None, transport=None):
    settings = settings or Settings.from_env()
    tasks = {}

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = RequestStore(settings.db_path)
        app.state.add_slots = asyncio.Semaphore(settings.max_add_concurrency)
        app.state.search_slots = asyncio.Semaphore(settings.max_search_concurrency)
        async with httpx.AsyncClient(
            transport=transport, timeout=settings.upstream_timeout_seconds,
            follow_redirects=False, limits=httpx.Limits(max_connections=160,
                                                        max_keepalive_connections=80),
        ) as client:
            app.state.upstream = Mem0Upstream(settings, client)
            yield
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    app = FastAPI(title="LDBD Mem0 compatible service", version="reconstructed-2026.10.22",
                  lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(x_api_key: str | None = Header(default=None)):
        if not settings.wrapper_api_key:
            raise HTTPException(503, "WRAPPER_API_KEY is not configured")
        if not x_api_key or not hmac.compare_digest(x_api_key.encode(), settings.wrapper_api_key.encode()):
            raise HTTPException(401, "Invalid API key")
        if not settings.mem0_api_key:
            raise HTTPException(503, "MEM0_API_KEY is not configured")

    @app.get("/health")
    async def health():
        ready = bool(settings.wrapper_api_key and settings.mem0_api_key)
        return JSONResponse({"status": "ok" if ready else "not_configured"},
                            status_code=200 if ready else 503)

    @app.get("/live")
    async def live():
        return {"status": "ok"}

    async def perform_add(payload, existing, is_new):
        store = app.state.store
        user, request = payload["user_id"], payload["request_id"]
        try:
            async with app.state.add_slots:
                event_ids = json.loads(existing.get("event_ids") or "[]")
                if is_new or existing["state"] == "retryable":
                    store.update(user, request, "submitted")
                    result = await app.state.upstream.submit_add(payload)
                    event_ids = result.get("event_ids", [])
                    if event_ids:
                        store.update(user, request, "pending", event_ids=event_ids)
                    elif result.get("completed") is not True:
                        raise UpstreamError(502, "Unrecognized upstream add response", ambiguous=True)
                if event_ids:
                    await app.state.upstream.wait_events(event_ids)
                response = {"success": True, "request_id": request, "user_id": user,
                            "session_id": payload["session_id"]}
                store.update(user, request, "completed", response=response)
                return response
        except UpstreamError as exc:
            row = store.get(user, request)
            has_events = bool(row and json.loads(row.get("event_ids") or "[]"))
            if exc.ambiguous:
                state = "uncertain"
            elif exc.retryable:
                state = "pending" if has_events else "retryable"
            else:
                state = "failed"
            store.update(user, request, state, error=exc.detail, status_code=exc.status_code)
            raise HTTPException(exc.status_code, exc.detail) from None

    @app.post("/add", dependencies=[Depends(authorized)])
    @app.post("/v1/memories/add", dependencies=[Depends(authorized)])
    async def add(body: AddRequest):
        payload = body.model_dump(exclude_none=True, exclude={"async_mode"})
        key = (body.user_id, body.request_id)
        fingerprint = hashlib.sha256(json.dumps(
            {"payload": payload, "upstream": settings.mem0_base_url, "mode": settings.mem0_api_mode},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        store = app.state.store
        row, is_new = store.claim(*key, fingerprint)
        if row["fingerprint"] != fingerprint:
            raise HTTPException(409, "request_id is bound to a different payload")
        if row["state"] == "completed":
            return json.loads(row["response"])
        task = tasks.get(key)
        if task is None:
            if row["state"] in {"uncertain", "submitted"} and not is_new:
                raise HTTPException(503, "Previous add outcome is unknown; reconcile before resubmitting")
            if row["state"] == "failed":
                raise HTTPException(row["status_code"] or 502, row["error"] or "Upstream add failed")
            task = asyncio.create_task(perform_add(payload, row, is_new))
            tasks[key] = task

            def finished(done):
                tasks.pop(key, None)
                # Retrieve exceptions even when the HTTP client disconnected.
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    @app.post("/search", dependencies=[Depends(authorized)])
    @app.post("/v1/memories/search", dependencies=[Depends(authorized)])
    async def search(body: SearchRequest):
        options = body.search_options.model_dump(exclude_none=True) if body.search_options else {}
        try:
            async with app.state.search_slots:
                data = await app.state.upstream.search(body.query, body.user_id, body.top_k, options)
            return {"success": True, "data": data}
        except UpstreamError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None

    return app


app = create_app()
