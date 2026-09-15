# Oceane FastAPI backend

Python/FastAPI owns application endpoints; Supabase owns authentication and PostgreSQL. The first implemented feature is admin authentication, including the React login, recovery and password-reset pages. Fleet, booking and reporting data still use the existing frontend services and have not yet been migrated.

## 1. Configure Supabase

1. In your Supabase project, enable email/password authentication.
2. Run `migrations/001_admin_auth.sql` once in the Supabase SQL editor as the project owner. This creates the admin membership table and its Row Level Security policy.
3. In Authentication > Users, create your administrator with a strong password and a confirmed email. There is no default password or public admin registration.
4. Open `migrations/bootstrap_admin.sql.example`, replace `YOUR_ADMIN_EMAIL` and `YOUR_ADMIN_NAME`, and execute it in the SQL editor. Only project administrators should run this script. An ordinary Supabase user without an active membership cannot enter the admin application.
5. In Authentication > URL Configuration, set your development site URL to `http://localhost:3000` and allow `http://localhost:3000/admin/reset-password` as a redirect URL. Keep the standard recovery email confirmation link. Configure your email provider/SMTP for real password-recovery delivery. Add the exact HTTPS production reset URL when deploying.

Use the same hostname throughout a session; localhost and 127.0.0.1 have separate cookies. If you choose 127.0.0.1, update FRONTEND_ORIGIN and the Supabase recovery redirect accordingly. Public signup can be disabled if this project only needs staff accounts; membership checks remain required regardless.

## 2. Start the backend

From this backend directory in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Edit `.env` with your project's `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (the publishable key or legacy anon key). Keep the default development origins and port unless you change the frontend URL. No service-role key is needed for this authentication implementation; do not substitute one for the publishable key. Never commit `.env`.

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health: `http://127.0.0.1:8000/api/health`. Development API schema: `http://127.0.0.1:8000/docs`. An unconfigured development server can start, but authentication returns a configuration error and denies access.

## 3. Start the frontend

In a second terminal, from the sibling `Car-rental1` directory:

```powershell
npm install
npm run dev
```

Open `http://localhost:3000/admin/login`. Vite proxies `/api` to FastAPI on port 8000, keeping browser requests on one origin. `API_PROXY_TARGET` can override the target as a process environment variable for development. No Supabase credentials are needed in the frontend.

## Implemented behavior

| Endpoint | Purpose |
| --- | --- |
| POST /api/admin/auth/login | Verify Supabase credentials, confirmed email and active admin membership; issue HTTP-only cookies |
| GET /api/admin/auth/me | Verify the current Supabase user and recheck membership |
| POST /api/admin/auth/refresh | Rotate the Supabase session and recheck membership |
| POST /api/admin/auth/logout | Revoke the current session and clear browser cookies |
| POST /api/admin/auth/forgot-password | Request a recovery email with a fixed, configured redirect |
| POST /api/admin/auth/reset-password | Validate the recovery bearer and membership, change password and revoke sessions |

The frontend restores sessions, refreshes expired access, synchronizes logout between tabs and rechecks access on focus and periodically. Protected pages wait for verification. Identity and role come from Supabase and the owner-managed membership table; the former mock role switcher is removed. Password recovery removes the token fragment from the visible URL and holds it only in memory.

Cookies are HTTP-only and SameSite=Lax. Mutation requests require an allowed Origin, JSON content and `X-Requested-With: XMLHttpRequest`. Request bodies are limited to 16 KiB; errors do not echo passwords. Login/recovery operations have a process-local rate limiter. Authentication responses are not cacheable.

To deactivate an administrator, set `admin_memberships.is_active = false` using the SQL editor. The next protected API request is denied. The UI detects this at its next session check. Both supported roles (`admin`, `super_admin`) currently enter the same application; granular operational permissions and user-management endpoints are future work. Editing membership is deliberately unavailable to ordinary authenticated clients.

## Protect future endpoints

Use the shared dependency on every administrative endpoint; hiding a React page alone does not authorize database operations:

```python
from fastapi import APIRouter
from app.auth import Admin

router = APIRouter(prefix="/api/admin")

@router.get("/example")
async def example(admin: Admin):
    return {"administrator_id": str(admin.id)}
```

Register new routers in `app/main.py`, add appropriate database policies and perform authoritative validation in the backend. Future privileged operations must also enforce the required role. Supabase calls use a stateless HTTP client with each user's bearer token so sessions are not shared between requests.

## Verification

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

The backend tests mock the external Supabase HTTP boundary. They cover credentials, membership revocation, session refresh/logout, recovery, origin checks, limits and upstream failures. Frontend checks are `npm run lint`, `npm test` and `npm run build`.

The optional browser suite is `Car-rental1/tests/admin-auth.e2e.mjs`. It requires the existing local `.review-tools/node_modules/playwright` installation and Microsoft Edge. Run `uvicorn tests.browser_app:app --host 127.0.0.1 --port 8001` using the virtual environment, start Vite with `$env:API_PROXY_TARGET = 'http://127.0.0.1:8001'`, then run `node tests/admin-auth.e2e.mjs` from the frontend directory. This fixture contains fake credentials and test-control endpoints: use it only for local tests, never deploy it. Stop both test servers afterward and clear API_PROXY_TARGET before normal development.

No live Supabase migration or email delivery has been performed by these tests. After configuration, verify real admin sign-in, rejection of a non-admin, recovery email delivery, password reset, refresh, logout and membership deactivation against your development project.

## Production deployment

- Deploy `app.main:app` and the frontend under one HTTPS origin. Route `/api/*` to FastAPI and other routes to the built frontend, with SPA fallback for `/admin/login` and `/admin/reset-password`. Vite's proxy is only for local development/preview.
- Set APP_ENV=production, COOKIE_SECURE=true, FRONTEND_ORIGIN to the HTTPS site, ALLOWED_ORIGINS to an exact JSON list of trusted HTTPS origins, and ALLOWED_HOSTS to the actual hostnames accepted by the API. Development API documentation is disabled in production.
- Store backend configuration in your hosting provider's secret/environment settings. Configure trusted proxy addresses explicitly; do not trust arbitrary forwarded client IPs. Add shared rate limiting at the gateway or Redis before using multiple workers/instances, since the included limiter is process-local.
- Keep TLS, SMTP, monitoring and database backups configured. Do not log passwords, cookies, authorization headers or recovery tokens. Supabase access JWTs can remain valid until expiry after session revocation; membership checks provide the immediate authorization cutoff for this application's protected endpoints.
- Password reset attempts global session revocation after changing the password. If revocation is unavailable, the response reports that the password changed but other sessions could not be signed out. Logout clears local cookies even when the provider cannot be reached.

Supabase references: [Password authentication and recovery](https://supabase.com/docs/guides/auth/passwords), [Auth HTTP API](https://github.com/supabase/auth), [Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security).
