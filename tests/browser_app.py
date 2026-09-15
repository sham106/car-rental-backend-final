"""Isolated browser test fixture. Never used by app.main or production deployment."""

import httpx

from app.config import Settings
from app.main import create_app
from tests.test_auth import Provider

provider = Provider()
settings = Settings(
    _env_file=None,
    app_env="test",
    supabase_url="https://project.example.com",
    supabase_publishable_key="test-publishable",
    auth_rate_limit=100,
    allowed_hosts=["localhost", "127.0.0.1"],
)
app = create_app(settings, transport=httpx.MockTransport(provider))


@app.post("/__test__/state")
async def state(body: dict):
    for key in ("active", "member", "confirmed", "down", "bad_login", "expired", "bad_refresh"):
        if key in body:
            setattr(provider, key, bool(body[key]))
    return {"ok": True}
