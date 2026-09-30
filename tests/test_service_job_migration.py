import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.fleet_store import SupabaseStore


@pytest.mark.parametrize("code,message", [
    ("P0001", "Unknown resource"),
    ("42P01", 'relation "public.fleet_service_jobs" does not exist'),
])
def test_missing_service_job_migration_has_actionable_error(code, message):
    async def run():
        config = SimpleNamespace(
            supabase_url="https://test.example", supabase_secret_key=SecretStr("sb_secret_test")
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"code": code, "message": message})
        )) as client:
            store = SupabaseStore(config, client)
            with pytest.raises(HTTPException) as error:
                await store.rpc("fleet_commit", {"changes": [{"resource": "service_jobs"}]})
            assert error.value.status_code == 503
            assert "006_service_jobs.sql" in error.value.detail
    asyncio.run(run())
