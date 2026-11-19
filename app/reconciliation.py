"""Reconcile already-submitted event IDs. This module never submits memories."""
import asyncio
import json
import logging


class EventReconciler:
    def __init__(self, store, upstream, active_tasks=None, *, batch_size=100,
                 request_timeout_seconds=10, batch_timeout_seconds=30, concurrency=4):
        self.store = store
        self.upstream = upstream
        self.active_tasks = active_tasks if active_tasks is not None else {}
        self.batch_size = batch_size
        self.request_timeout_seconds = request_timeout_seconds
        self.batch_timeout_seconds = batch_timeout_seconds
        self.concurrency = concurrency
        self.lock = asyncio.Lock()
        self.last_summary = {}

    async def reconcile_once(self, request_ids=None):
        async with self.lock:
            summary = dict(checked=0, completed=0, deferred=0, errors=0, skipped=0,
                           timed_out=0)
            slots = asyncio.Semaphore(self.concurrency)

            async def inspect(row):
                async with slots:
                    key = (row["user_id"], row["request_id"])
                    # Rotate active and unsuccessful rows too, so they cannot
                    # permanently occupy the front of a bounded batch.
                    self.store.touch_reconciliation(*key)
                    if key in self.active_tasks:
                        summary["skipped"] += 1
                        return
                    current = self.store.get(*key)
                    if not current or current["state"] == "completed":
                        return
                    try:
                        if current.get("part_request_ids"):
                            summary["checked"] += 1
                            receipt = self.store.completed_parts_receipt(*key)
                            if receipt is not None:
                                self.store.complete(*key, receipt, reconciled=True)
                                summary["completed"] += 1
                            else:
                                summary["deferred"] += 1
                            return
                        raw_events = json.loads(current["event_ids"] or "[]")
                        if (not isinstance(raw_events, list) or not raw_events
                                or any(not isinstance(item, str) or not item.strip()
                                       for item in raw_events)):
                            raise ValueError("Invalid tracked event IDs")
                        events = list(dict.fromkeys(raw_events))
                        cursor = current["reconcile_event_cursor"] % len(events)
                        ordered = events[cursor:] + events[:cursor]
                        confirmed = json.loads(current["confirmed_event_ids"] or "[]")
                        remaining = [event for event in ordered if event not in confirmed]
                        summary["checked"] += 1
                        # Check at most four events per row per pass, saving each
                        # confirmed result and advancing before every GET. Slow
                        # or failed events cannot starve the end of a long list.
                        for event in remaining[:4]:
                            current = self.store.record_event_check(*key, event)
                            if current["state"] == "completed":
                                return
                            receipt = json.loads(current["receipt"]) if current.get("receipt") else None
                            proof = None if current.get("receipt_origin") == "event" else receipt
                            try:
                                result = await asyncio.wait_for(self.upstream.inspect_events(
                                    [event], user_id=key[0], request_id=key[1], receipt=proof),
                                    timeout=self.request_timeout_seconds)
                                if not result["completed"]:
                                    continue
                                if proof is None:
                                    # The legacy event's payload must prove the
                                    # user, request and session before checkpointing.
                                    session = result.get("session_id")
                                    if not isinstance(session, str) or not session.strip():
                                        raise ValueError("Missing verified event session")
                                    receipt = {"success": True, "user_id": key[0],
                                               "request_id": key[1], "session_id": session}
                                self.store.record_event_check(*key, event, receipt=receipt, completed=True)
                            except Exception:
                                summary["errors"] += 1
                        current = self.store.get(*key)
                        if current and current["state"] != "completed":
                            confirmed = json.loads(current["confirmed_event_ids"] or "[]")
                            if all(event in confirmed for event in events):
                                self.store.complete(*key, json.loads(current["receipt"]), reconciled=True)
                                summary["completed"] += 1
                            else:
                                summary["deferred"] += 1
                    except Exception:
                        # Never log an upstream body, message, key or raw exception.
                        # Failed probes leave the request eligible for a later GET.
                        summary["errors"] += 1

            jobs = [asyncio.create_task(inspect(row)) for row in
                    self.store.reconciliation_candidates(self.batch_size, request_ids)]
            try:
                if jobs:
                    _, pending = await asyncio.wait(jobs, timeout=self.batch_timeout_seconds)
                    summary["timed_out"] = len(pending)
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                results = await asyncio.gather(*jobs, return_exceptions=True)
                summary["errors"] += sum(isinstance(value, Exception) for value in results)
            self.last_summary = summary
            return summary

    async def run(self, interval_seconds):
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                result = await self.reconcile_once()
                if result["checked"] or result["errors"]:
                    logging.getLogger("uvicorn.error").info(
                        "Mem0 event reconciliation: checked=%d completed=%d deferred=%d errors=%d",
                        result["checked"], result["completed"], result["deferred"], result["errors"])
            except Exception:
                logging.getLogger("uvicorn.error").error("Mem0 event reconciliation cycle failed")
