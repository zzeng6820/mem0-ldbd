"""Reconcile stored Mem0 events once using GET only; never resubmit memories."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.umask(0o077)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv
from app.config import Settings
from app.reconciliation import EventReconciler
from app.store import RequestStore
from app.upstream import Mem0Upstream


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", action="append", help="Limit to an existing request ID")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    settings = Settings.from_env()
    if not settings.mem0_api_key:
        parser.error("MEM0_API_KEY is not configured")
    store = RequestStore(settings.db_path)
    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds,
                                 follow_redirects=False) as client:
        reconciler = EventReconciler(store, Mem0Upstream(settings, client),
                                    batch_size=settings.reconcile_batch_size)
        result = await reconciler.reconcile_once(args.request_id)
    print(json.dumps(result))
    return 1 if result["errors"] or result["timed_out"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
