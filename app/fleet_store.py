"""Supabase persistence with optimistic, atomic multi-entity commits.

Only the server key can execute these RPCs. A database revision protects validation
across multiple API workers: conflicts reload and revalidate the entire operation.
"""

from copy import deepcopy

import httpx
from fastapi import HTTPException

RESOURCES = (
    "owners",
    "vehicles",
    "customers",
    "bookings",
    "assignments",
    "maintenance",
    "service_jobs",
    "compliance",
    "files",
    "documents",
    "settings",
    "categories",
    "locations",
    "audit",
    "notifications",
    "outbox",
    "idempotency",
)


class SupabaseStore:
    def __init__(self, settings, client):
        self.settings = settings
        self.client = client

    def headers(self):
        key = self.settings.supabase_secret_key.get_secret_value()
        if not key or "YOUR_" in key:
            raise HTTPException(503, "Configure SUPABASE_SECRET_KEY in the backend environment.")
        result = {"apikey": key}
        if not key.startswith("sb_secret_"):
            result["Authorization"] = f"Bearer {key}"
        return result

    async def rpc(self, function, data):
        try:
            response = await self.client.post(
                self.settings.supabase_url + "/rest/v1/rpc/" + function,
                headers=self.headers(),
                json=data,
            )
        except httpx.RequestError:
            raise HTTPException(503, "Database temporarily unavailable. Please retry.") from None
        if response.status_code >= 400:
            error = response.json() if "json" in response.headers.get("content-type", "") else {}
            code = (
                error.get("code")
                if "json" in response.headers.get("content-type", "")
                else ""
            )
            if code in {"23505", "23P01", "40001"}:
                raise HTTPException(409, "A conflicting record was saved. Refresh and try again.")
            if code == "23503":
                raise HTTPException(409, "This record is still referenced by another operation.")
            if (
                code == "P0001"
                and error.get("message") == "Unknown resource"
                and function == "fleet_commit"
                and any(change.get("resource") == "service_jobs" for change in data.get("changes", []))
            ) or (code == "42P01" and "fleet_service_jobs" in error.get("message", "")):
                raise HTTPException(
                    503,
                    "Service jobs are not set up in the database. Run backend migration "
                    "006_service_jobs.sql in this backend's Supabase project, then retry.",
                )
            if code in {"PGRST202", "PGRST205", "42P01"}:
                raise HTTPException(503, "Run backend migration 002_fleet_backend.sql in Supabase.")
            raise HTTPException(503, "Database access unavailable. Check backend configuration.")
        return response.json()

    async def snapshot(self):
        result = await self.rpc("fleet_snapshot", {})
        return result["revision"], {
            r: {x["id"]: x for x in result["records"].get(r, [])} for r in RESOURCES
        }

    async def commit(self, revision, before, after, allocations):
        changes = []
        for resource in RESOURCES:
            for key, value in after[resource].items():
                if before[resource].get(key) != value:
                    changes.append({"resource": resource, "id": key, "data": value})
            for key in before[resource].keys() - after[resource].keys():
                changes.append({"resource": resource, "id": key, "data": None})
        if not changes:
            return True
        return await self.rpc(
            "fleet_commit",
            {
                "expected_revision": revision,
                "changes": changes,
                "allocations": allocations,
            },
        )

    async def transact(self, operation):
        for _ in range(5):
            revision, before = await self.snapshot()
            after = deepcopy(before)
            result, allocations = operation(after)
            if await self.commit(revision, before, after, allocations):
                return result
        raise HTTPException(409, "Fleet changed while saving. Refresh and retry the operation.")
