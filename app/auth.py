from collections import OrderedDict, deque
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings
from .schemas import AdminIdentity, LoginInput, RecoveryInput, ResetPasswordInput, SessionResponse
from .supabase_auth import SupabaseAuth

ACCESS_COOKIE = "ocr_admin_access"
REFRESH_COOKIE = "ocr_admin_refresh"
AUTH_PATH = "/api/admin/auth"
router = APIRouter(prefix=AUTH_PATH, tags=["Admin authentication"])


class AttemptLimiter:
    # Single-process development guard. Use shared gateway/Redis limits for multiple workers.
    def __init__(self):
        self.attempts: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str, limit: int):
        now = monotonic()
        entries = self.attempts.setdefault(key, deque())
        self.attempts.move_to_end(key)
        while entries and entries[0] <= now - 60:
            entries.popleft()
        if len(entries) >= limit:
            raise HTTPException(
                429,
                "Too many attempts. Please try again in a minute.",
                headers={"Retry-After": "60"},
            )
        entries.append(now)
        while len(self.attempts) > 10000:
            self.attempts.popitem(last=False)


def service(request: Request) -> SupabaseAuth:
    return request.app.state.auth


def limited(request: Request):
    # Do not trust arbitrary X-Forwarded-For headers. Configure trusted proxies at deployment.
    address = request.client.host if request.client else "unknown"
    request.app.state.limiter.check(address, request.app.state.settings.auth_rate_limit)


def clear_cookies(response: Response, settings: Settings):
    for name, path in [(ACCESS_COOKIE, "/api"), (REFRESH_COOKIE, AUTH_PATH)]:
        response.delete_cookie(
            name, path=path, secure=settings.cookie_secure, httponly=True, samesite="lax"
        )


def session_tokens(data: dict) -> tuple[str, str, int]:
    if not isinstance(data, dict):
        raise HTTPException(503, "Invalid authentication service response.")
    access, refresh = data.get("access_token"), data.get("refresh_token")
    expires = data.get("expires_in")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise HTTPException(503, "Invalid authentication service response.")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        raise HTTPException(503, "Invalid session expiry.")
    return access, refresh, min(expires, 86400)


async def establish(data: dict, auth: SupabaseAuth) -> JSONResponse:
    access, refresh, expires = session_tokens(data)
    try:
        identity = await auth.identity(access)
    except HTTPException:
        try:
            await auth.revoke(access)
        except HTTPException:
            pass
        raise
    response = JSONResponse(
        SessionResponse(user=identity, expires_in=expires).model_dump(mode="json")
    )
    response.set_cookie(
        ACCESS_COOKIE,
        access,
        max_age=expires,
        path="/api",
        secure=auth.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=auth.settings.session_max_age,
        path=AUTH_PATH,
        secure=auth.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


async def require_admin(
    request: Request, auth: Annotated[SupabaseAuth, Depends(service)]
) -> AdminIdentity:
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise HTTPException(401, "Please sign in to continue.")
    return await auth.identity(token)


Admin = Annotated[AdminIdentity, Depends(require_admin)]
Auth = Annotated[SupabaseAuth, Depends(service)]


@router.post("/login", response_model=SessionResponse, dependencies=[Depends(limited)])
async def login(body: LoginInput, auth: Auth):
    return await establish(
        await auth.login(str(body.email), body.password.get_secret_value()), auth
    )


@router.get("/me", response_model=AdminIdentity)
async def me(admin: Admin):
    return admin


@router.post("/refresh", response_model=SessionResponse)
async def refresh(request: Request, auth: Auth):
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(401, "Your session has ended. Please sign in again.")
    return await establish(await auth.refresh(token), auth)


@router.post("/logout", status_code=204)
async def logout(request: Request, auth: Auth):
    response: Response = Response(status_code=204)
    access, refresh_token = request.cookies.get(ACCESS_COOKIE), request.cookies.get(REFRESH_COOKIE)
    try:
        if access:
            try:
                await auth.revoke(access)
                refresh_token = None
            except HTTPException as error:
                if error.status_code != 401:
                    raise
        if refresh_token:
            try:
                tokens = await auth.refresh(refresh_token)
                await auth.revoke(session_tokens(tokens)[0])
            except HTTPException as error:
                if error.status_code != 401:
                    raise
    except HTTPException:
        response = JSONResponse(
            {
                "detail": (
                    "Signed out of this browser, but the server session could not be revoked. "
                    "Contact your administrator if you suspect account access."
                )
            },
            status_code=503,
        )
    clear_cookies(response, auth.settings)
    return response


@router.post("/forgot-password", dependencies=[Depends(limited)])
async def forgot_password(body: RecoveryInput, auth: Auth):
    await auth.recover(str(body.email))
    return {
        "message": "If the account exists, a password reset link will be sent to its email address."
    }


@router.post("/reset-password", dependencies=[Depends(limited)])
async def reset_password(body: ResetPasswordInput, auth: Auth):
    token = body.access_token.get_secret_value()
    await auth.identity(token)
    await auth.request(
        "PUT", "/auth/v1/user", token=token, payload={"password": body.password.get_secret_value()}
    )
    # Password updates must not be reported as failed just because revocation is unavailable.
    message = "Password updated. You can now sign in with your new password."
    try:
        await auth.revoke(token, scope="global")
    except HTTPException:
        message += " Other sessions could not be signed out; contact your administrator if needed."
    response = JSONResponse({"message": message})
    clear_cookies(response, auth.settings)
    return response
