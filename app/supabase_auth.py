from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from .config import Settings
from .schemas import AdminIdentity


class SupabaseAuth:
    # Stateless HTTP requests: a shared SDK auth session must never mix users' credentials.
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        if not self.settings.auth_configured:
            raise HTTPException(
                503, "Admin authentication is not configured. Contact the operator."
            )
        headers = {"apikey": self.settings.supabase_publishable_key.get_secret_value()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            result = await self.client.request(
                method,
                self.settings.supabase_url + path,
                headers=headers,
                json=payload,
                params=params,
            )
        except httpx.RequestError:
            raise HTTPException(503, "Authentication service is temporarily unavailable.") from None
        if result.status_code == 429:
            raise HTTPException(
                429, "Too many attempts. Please try again later.", headers={"Retry-After": "60"}
            )
        if result.status_code >= 500:
            raise HTTPException(503, "Authentication service is temporarily unavailable.")
        if result.status_code >= 400:
            if path.startswith("/rest/"):
                if result.status_code == 401:
                    raise HTTPException(401, "Your session has expired. Please sign in again.")
                raise HTTPException(503, "Admin access configuration is unavailable.")
            if path == "/auth/v1/user" and method == "PUT":
                raise HTTPException(
                    400,
                    (
                        "Password could not be changed. Use a different, strong password "
                        "or request a new link."
                    ),
                )
            raise HTTPException(401, "Invalid credentials or expired session.")
        if result.status_code == 204 or not result.content:
            return None
        try:
            return result.json()
        except ValueError:
            raise HTTPException(503, "Invalid authentication service response.") from None

    async def identity(self, token: str) -> AdminIdentity:
        user = await self.request("GET", "/auth/v1/user", token=token)
        if not isinstance(user, dict) or not user.get("id") or not user.get("email_confirmed_at"):
            raise HTTPException(403, "A verified administrator account is required.")
        # RLS permits only this user's membership to be read. No service-role key is needed.
        rows = await self.request(
            "GET",
            "/rest/v1/admin_memberships",
            token=token,
            params={
                "select": "user_id,display_name,role,is_active",
                "user_id": f"eq.{user['id']}",
                "is_active": "eq.true",
                "limit": "1",
            },
        )
        if not isinstance(rows, list):
            raise HTTPException(503, "Invalid admin access configuration.")
        if (
            not rows
            or rows[0].get("is_active") is not True
            or (
                rows[0].get("user_id") != user["id"]
                or rows[0].get("role") not in {"super_admin", "admin"}
            )
        ):
            raise HTTPException(403, "This account does not have active administrator access.")
        try:
            return AdminIdentity(
                id=user["id"],
                email=user["email"],
                name=rows[0].get("display_name") or user["email"],
                role=rows[0]["role"],
            )
        except (KeyError, ValidationError):
            raise HTTPException(503, "Invalid administrator profile.") from None

    async def login(self, email: str, password: str) -> dict:
        return await self.request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "password"},
            payload={"email": email, "password": password},
        )

    async def refresh(self, token: str) -> dict:
        return await self.request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            payload={"refresh_token": token},
        )

    async def revoke(self, token: str, scope: str = "local") -> None:
        await self.request("POST", "/auth/v1/logout", token=token, params={"scope": scope})

    async def recover(self, email: str) -> None:
        try:
            await self.request(
                "POST",
                "/auth/v1/recover",
                payload={"email": email},
                params={"redirect_to": self.settings.frontend_origin + "/admin/reset-password"},
            )
        except HTTPException as error:
            # Avoid exposing whether an email exists. Outages and throttling remain visible.
            if error.status_code != 401:
                raise
