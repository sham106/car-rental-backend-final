"""Deliver queued events to an operator-configured webhook. Run --once via a scheduler.

At-least-once delivery: receivers must deduplicate the event ID. A lease prevents
multiple workers from normally sending the same event simultaneously.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from .config import get_settings
from .fleet_domain import allocations, now
from .fleet_store import SupabaseStore


async def deliver_once(store, client, config):
    if (
        not config.notification_webhook_url
        or not config.notification_webhook_secret.get_secret_value()
    ):
        return 0
    lease = str(uuid4())

    def claim(state):
        candidates = [
            x
            for x in state["outbox"].values()
            if (x["status"] == "pending" and x.get("nextAttemptAt", "") <= now())
            or (x["status"] == "sending" and x.get("leaseUntil", "") < now())
        ]
        if not candidates:
            return None, allocations(state)
        row = sorted(candidates, key=lambda x: x["createdAt"])[0]
        row.update(
            status="sending",
            leaseId=lease,
            leaseUntil=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        )
        return dict(row), allocations(state)

    row = await store.transact(claim)
    if not row:
        return 0
    body = json.dumps(
        {"id": row["id"], "event": row["event"], "payload": row["payload"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        config.notification_webhook_secret.get_secret_value().encode(), body, hashlib.sha256
    ).hexdigest()
    success = False
    try:
        response = await client.post(
            config.notification_webhook_url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Oceane-Signature": signature,
                "Idempotency-Key": row["id"],
            },
        )
        success = 200 <= response.status_code < 300
    except httpx.RequestError:
        pass

    def finish(state):
        current = state["outbox"].get(row["id"])
        if current and current.get("leaseId") == lease:
            attempts = current.get("attempts", 0) + 1
            current.update(
                status="delivered" if success else "failed" if attempts >= 10 else "pending",
                attempts=attempts,
                lastAttemptAt=now(),
                nextAttemptAt=(
                    datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2**attempts))
                ).isoformat(),
            )
        return None, allocations(state)

    await store.transact(finish)
    return 1


async def run(once):
    config = get_settings()
    if (
        not config.notification_webhook_url
        or not config.notification_webhook_secret.get_secret_value()
    ):
        raise SystemExit(
            "Configure NOTIFICATION_WEBHOOK_URL and NOTIFICATION_WEBHOOK_SECRET first."
        )
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        store = SupabaseStore(config, client)
        while True:
            count = await deliver_once(store, client, config)
            if once:
                print(f"Processed {count} queued event(s).")
                return
            if not count:
                await asyncio.sleep(15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(run(parser.parse_args().once))
