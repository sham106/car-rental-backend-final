import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import ACCESS_COOKIE, REFRESH_COOKIE
from app.config import Settings
from app.main import create_app

USER_ID = "11111111-1111-4111-8111-111111111111"
ORIGIN = "http://localhost:3000"
HEADERS = {"Origin": ORIGIN, "X-Requested-With": "XMLHttpRequest"}
LOGIN = {"email": "admin@example.com", "password": "a-good-test-password"}


class Provider:
    def __init__(self):
        self.active = True
        self.member = True
        self.confirmed = True
        self.role = "super_admin"
        self.down = False
        self.bad_login = False
        self.expired = False
        self.bad_refresh = False
        self.calls = []
        self.revoked = []
        self.new_password = None

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if self.down:
            return httpx.Response(503, json={"message": "private provider error"})
        assert request.headers["apikey"] == "test-publishable"
        token = request.headers.get("authorization", "")
        if path == "/auth/v1/token":
            grant = request.url.params.get("grant_type")
            data = json.loads(request.content)
            if grant == "password" and (self.bad_login or data != LOGIN):
                return httpx.Response(400, json={"error": "invalid_grant"})
            if grant == "refresh_token" and (
                self.bad_refresh or data["refresh_token"] != "refresh-valid"
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "access-fresh" if grant == "refresh_token" else "access-valid",
                    "refresh_token": "refresh-valid",
                    "expires_in": 3600,
                },
            )
        if path == "/auth/v1/user":
            if token not in {
                "Bearer access-valid",
                "Bearer access-fresh",
                "Bearer recovery-valid",
            } or (self.expired and token == "Bearer access-valid"):
                return httpx.Response(401, json={"error": "expired"})
            if request.method == "PUT":
                self.new_password = json.loads(request.content)["password"]
            return httpx.Response(
                200,
                json={
                    "id": USER_ID,
                    "email": LOGIN["email"],
                    "email_confirmed_at": "2026-09-08T00:00:00Z" if self.confirmed else None,
                    "user_metadata": {"role": "super_admin"},
                },
            )
        if path == "/rest/v1/admin_memberships":
            assert token.startswith("Bearer access-") or token == "Bearer recovery-valid"
            assert request.url.params["user_id"] == f"eq.{USER_ID}"
            return httpx.Response(
                200,
                json=[
                    {
                        "user_id": USER_ID,
                        "display_name": "Real Admin",
                        "role": self.role,
                        "is_active": self.active,
                    }
                ]
                if self.member
                else [],
            )
        if path == "/auth/v1/logout":
            if self.expired and token == "Bearer access-valid":
                return httpx.Response(401, json={"error": "expired"})
            self.revoked.append((token, request.url.params.get("scope")))
            return httpx.Response(204)
        if path == "/auth/v1/recover":
            assert request.url.params["redirect_to"] == ORIGIN + "/admin/reset-password"
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected provider request: {request.method} {path}")


@contextmanager
def client_for(provider=None, **overrides):
    provider = provider or Provider()
    settings = Settings(
        _env_file=None,
        app_env="test",
        supabase_url="https://project.example.com",
        supabase_publishable_key="test-publishable",
        allowed_hosts=["testserver"],
        **overrides,
    )
    app = create_app(settings, transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        yield client, provider


def post(client, path, payload=None, **kwargs):
    return client.post(
        "/api/admin/auth/" + path,
        json={} if payload is None else payload,
        headers=HEADERS,
        **kwargs,
    )


def test_anonymous_me_denied_without_provider_call():
    with client_for() as (client, provider):
        assert client.get("/api/admin/auth/me").status_code == 401
        assert not provider.calls


def test_login_sets_http_only_cookies_and_returns_no_tokens():
    with client_for() as (client, provider):
        response = post(client, "login", LOGIN)
        assert response.status_code == 200
        assert response.json()["user"] == {
            "id": USER_ID,
            "name": "Real Admin",
            "email": LOGIN["email"],
            "role": "super_admin",
        }
        assert "access_token" not in response.text and "password" not in response.text
        assert client.cookies.get(ACCESS_COOKIE) == "access-valid"
        assert client.cookies.get(REFRESH_COOKIE) == "refresh-valid"
        for cookie in response.headers.get_list("set-cookie"):
            assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "Path=/api/admin/auth" in response.headers.get_list("set-cookie")[1]
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/admin/auth/me").status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("bad_login", True),
        ("member", False),
        ("active", False),
        ("confirmed", False),
        ("role", "viewer"),
    ],
)
def test_invalid_or_unauthorized_login_does_not_issue_session(field, value):
    provider = Provider()
    setattr(provider, field, value)
    with client_for(provider) as (client, _):
        response = post(client, "login", LOGIN)
        assert response.status_code == (401 if field == "bad_login" else 403)
        assert not client.cookies.get(ACCESS_COOKIE)
        assert not client.cookies.get(REFRESH_COOKIE)
        if field != "bad_login":
            assert provider.revoked


def test_membership_revocation_is_checked_each_request():
    with client_for() as (client, provider):
        post(client, "login", LOGIN)
        provider.active = False
        assert client.get("/api/admin/auth/me").status_code == 403
        assert not client.cookies.get(ACCESS_COOKIE)
        assert not client.cookies.get(REFRESH_COOKIE)


def test_forged_cookie_is_verified_by_supabase():
    with client_for() as (client, provider):
        client.cookies.set(ACCESS_COOKIE, "forged")
        assert client.get("/api/admin/auth/me").status_code == 401
        assert len(provider.calls) == 1


def test_expired_access_can_refresh_and_rotates_cookie():
    with client_for() as (client, provider):
        post(client, "login", LOGIN)
        provider.expired = True
        assert client.get("/api/admin/auth/me").status_code == 401
        assert client.cookies.get(REFRESH_COOKIE)
        assert post(client, "refresh").status_code == 200
        assert client.cookies.get(ACCESS_COOKIE) == "access-fresh"
        assert client.get("/api/admin/auth/me").status_code == 200


def test_expired_refresh_clears_cookies():
    with client_for() as (client, provider):
        post(client, "login", LOGIN)
        provider.bad_refresh = True
        assert post(client, "refresh").status_code == 401
        assert not client.cookies.get(REFRESH_COOKIE)


def test_refresh_requires_active_membership():
    with client_for() as (client, provider):
        post(client, "login", LOGIN)
        provider.active = False
        assert post(client, "refresh").status_code == 403
        assert not client.cookies.get(ACCESS_COOKIE)


@pytest.mark.parametrize("expired", [False, True])
def test_logout_revokes_session_and_deletes_cookies(expired):
    with client_for() as (client, provider):
        post(client, "login", LOGIN)
        provider.expired = expired
        assert post(client, "logout").status_code == 204
        assert provider.revoked[-1][1] == "local"
        assert not client.cookies.get(ACCESS_COOKIE)
        assert not client.cookies.get(REFRESH_COOKIE)
        assert post(client, "refresh").status_code == 401


def test_logout_outage_still_clears_browser_cookies():
    with client_for() as (client, provider):
        post(client, "login", LOGIN)
        provider.down = True
        assert post(client, "logout").status_code == 503
        assert not client.cookies.get(ACCESS_COOKIE)
        assert not client.cookies.get(REFRESH_COOKIE)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://attacker.example", "X-Requested-With": "XMLHttpRequest"},
        {"Origin": ORIGIN},
    ],
)
def test_cross_site_or_missing_csrf_headers_rejected(headers):
    with client_for() as (client, provider):
        response = client.post("/api/admin/auth/login", json=LOGIN, headers=headers)
        assert response.status_code == 403
        assert not provider.calls


def test_invalid_payload_does_not_echo_password_or_token():
    with client_for() as (client, _):
        response = post(client, "login", {"email": "invalid", "password": "PRIVATE_PASSWORD"})
        assert response.status_code == 422
        assert "PRIVATE_PASSWORD" not in response.text
        response = post(
            client, "reset-password", {"access_token": "PRIVATE_TOKEN", "password": "tiny"}
        )
        assert response.status_code == 422
        assert "PRIVATE_TOKEN" not in response.text


def test_request_body_limit_and_content_type():
    with client_for() as (client, provider):
        assert post(client, "login", {"password": "x" * 17000}).status_code == 413
        assert client.post("/api/admin/auth/login", data=LOGIN, headers=HEADERS).status_code == 415
        assert not provider.calls


def test_rate_limit_blocks_repeated_login_requests():
    with client_for(auth_rate_limit=2) as (client, provider):
        provider.bad_login = True
        assert post(client, "login", LOGIN).status_code == 401
        assert post(client, "login", LOGIN).status_code == 401
        result = post(client, "login", LOGIN)
        assert result.status_code == 429 and result.headers["retry-after"] == "60"


def test_provider_outage_does_not_leak_details_or_grant_access():
    with client_for() as (client, provider):
        provider.down = True
        result = post(client, "login", LOGIN)
        assert result.status_code == 503
        assert "private provider error" not in result.text
        assert not client.cookies.get(ACCESS_COOKIE)


def test_password_recovery_uses_fixed_redirect_and_generic_message():
    with client_for() as (client, _):
        result = post(client, "forgot-password", {"email": "unknown@example.com"})
        assert result.status_code == 200
        assert "If the account exists" in result.json()["message"]


def test_reset_password_validates_token_and_revokes_all_sessions():
    with client_for() as (client, provider):
        result = post(
            client,
            "reset-password",
            {"access_token": "recovery-valid", "password": "brand-new-password"},
        )
        assert result.status_code == 200
        assert provider.new_password == "brand-new-password"
        assert provider.revoked[-1][1] == "global"
        assert "recovery-valid" not in result.text


def test_reset_requires_real_authorized_identity():
    with client_for() as (client, provider):
        assert (
            post(
                client,
                "reset-password",
                {"access_token": "forged", "password": "brand-new-password"},
            ).status_code
            == 401
        )
        provider.member = False
        assert (
            post(
                client,
                "reset-password",
                {"access_token": "recovery-valid", "password": "brand-new-password"},
            ).status_code
            == 403
        )
        assert provider.new_password is None


def test_health_and_unconfigured_auth_fail_closed():
    settings = Settings(_env_file=None, app_env="test", allowed_hosts=["testserver"])
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json() == {"status": "ok", "auth_configured": False}
        assert post(client, "login", LOGIN).status_code == 503


def test_production_requires_secure_configuration():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")


def test_production_cookie_flags():
    settings = Settings(
        _env_file=None,
        app_env="production",
        supabase_url="https://project.example.com",
        supabase_publishable_key="test-publishable",
        cookie_secure=True,
        frontend_origin="https://fleet.example.com",
        allowed_origins=["https://fleet.example.com"],
        allowed_hosts=["testserver"],
    )
    with TestClient(create_app(settings, transport=httpx.MockTransport(Provider()))) as client:
        result = client.post(
            "/api/admin/auth/login",
            json=LOGIN,
            headers={"Origin": "https://fleet.example.com", "X-Requested-With": "XMLHttpRequest"},
        )
        assert result.status_code == 200
        assert all("Secure" in c for c in result.headers.get_list("set-cookie"))
        assert client.get("/docs").status_code == 404
