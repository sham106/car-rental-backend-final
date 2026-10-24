"""Explicit operator tool; never exposed as a public application endpoint.

Default mode is read-only. Reset and invitation are separate to avoid deleting a
newly invited administrator when retrying email delivery.
"""
import argparse
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings

# Child tables must be removed before parents. Keep the schema and revision row.
TABLES = (
    "fleet_stock_movements", "fleet_stock_items", "fleet_allocations",
    "fleet_documents", "fleet_compliance", "fleet_service_jobs", "fleet_maintenance",
    "fleet_assignments", "fleet_bookings", "fleet_vehicles", "fleet_owners",
    "fleet_customers", "fleet_files", "fleet_notifications", "fleet_outbox",
    "fleet_idempotency", "fleet_settings", "fleet_categories", "fleet_locations", "fleet_audit",
)

class Operator:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            # Never print response bodies: auth responses may contain secrets or PII.
            raise RuntimeError(f"{method} {path.split('?')[0]} failed (HTTP {response.status_code}). No subsequent steps ran.")
        return response.json() if response.content else None

    def users(self):
        result = []
        page = 1
        while True:
            rows = self.request("GET", "/auth/v1/admin/users", params={"page": page, "per_page": 100})["users"]
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def plan(self):
        snapshot = self.request("POST", "/rest/v1/rpc/fleet_snapshot", json={})
        buckets = self.request("GET", "/storage/v1/bucket")
        users = self.users()
        # Validate permissions/table availability before beginning destructive steps.
        for table in TABLES:
            self.request("GET", f"/rest/v1/{table}", params={"select": "id", "limit": 1})
        self.request("GET", "/rest/v1/admin_memberships", params={"select": "user_id", "limit": 1})
        print("Record counts:", {k: len(v) for k, v in snapshot["records"].items()})
        print("Accounts:", len(users), "Storage buckets to empty:", [b["id"] for b in buckets])
        return snapshot, buckets, users

    def reset(self):
        snapshot, buckets, users = self.plan()
        self.request("DELETE", "/rest/v1/admin_memberships", params={"user_id": "not.is.null"})
        print("Removed all administrator memberships.", flush=True)
        for bucket in buckets:
            name = quote(bucket["id"], safe="")
            self.request("POST", f"/storage/v1/bucket/{name}/empty", json={})
            # Supabase may queue emptyBucket; wait for completion before deleting owners.
            for attempt in range(15):
                remaining = self.request("POST", f"/storage/v1/object/list/{name}", json={"prefix": "", "limit": 1})
                if not remaining:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Storage emptying has not completed; reset stopped before account deletion.")
        print("Storage buckets emptied; bucket configuration preserved.", flush=True)
        for user in users:
            self.request("DELETE", f"/auth/v1/admin/users/{quote(user['id'], safe='')}", json={"should_soft_delete": False})
        print("Removed existing authentication accounts.", flush=True)
        for table in TABLES:
            self.request("DELETE", f"/rest/v1/{table}", params={"id": "not.is.null"})
        revision = self.request("POST", "/rest/v1/rpc/fleet_snapshot", json={})["revision"]
        self.request("PATCH", "/rest/v1/fleet_revision", params={"singleton": "eq.true"}, json={"revision": revision + 1})
        after = self.request("POST", "/rest/v1/rpc/fleet_snapshot", json={})
        if any(after["records"].values()) or self.users():
            raise RuntimeError("Reset verification failed: data or accounts remain. Do not invite yet.")
        for table in ("fleet_allocations", "admin_memberships"):
            if self.request("GET", f"/rest/v1/{table}", params={"select": "*", "limit": 1}):
                raise RuntimeError(f"Reset verification failed: {table} is not empty.")
        print("Verified: all application records, memberships, and accounts removed.", flush=True)

    def invite(self, email, origin):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Use an exact HTTPS website origin without /admin or other paths.")
        existing = self.users()
        if existing:
            raise RuntimeError("Accounts already exist. Invitation stopped to avoid duplicating or replacing an administrator.")
        # Supabase sends the invite and creates an unconfirmed identity, then we grant membership.
        user = self.request("POST", "/auth/v1/invite", params={"redirect_to": origin + "/admin/reset-password"}, json={"email": email})
        if not isinstance(user, dict) or not user.get("id") or user.get("email", "").casefold() != email.casefold():
            raise RuntimeError("Unexpected invite response. Inspect Auth Users before retrying.")
        self.request("POST", "/rest/v1/admin_memberships", json={"user_id": user["id"], "display_name": "Shamsi", "role": "super_admin", "is_active": True})
        members = self.request("GET", "/rest/v1/admin_memberships", params={"user_id": "eq." + user["id"], "select": "role,is_active"})
        if members != [{"role": "super_admin", "is_active": True}]:
            raise RuntimeError("Invite sent but administrator membership verification failed.")
        print("Supabase accepted the invitation email and the super_admin membership is active. Email delivery is not independently verified.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "reset", "invite"])
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--origin")
    parser.add_argument("--email", default="shamsikush@gmail.com")
    args = parser.parse_args()
    settings = Settings()
    if urlsplit(settings.supabase_url).hostname != args.project_ref + ".supabase.co":
        raise ValueError("Project reference does not match backend configuration. No changes made.")
    if args.mode != "plan" and not args.apply:
        raise ValueError("Mutation requires --apply. Use plan to inspect first.")
    if args.mode == "invite" and not args.origin:
        raise ValueError("Invitation requires --origin.")
    secret = settings.supabase_secret_key.get_secret_value()
    if not secret:
        raise ValueError("Backend service key is missing.")
    headers = {"apikey": secret}
    if not secret.startswith("sb_secret_"):
        headers["Authorization"] = "Bearer " + secret
    with httpx.Client(base_url=settings.supabase_url, headers=headers, timeout=60, follow_redirects=False) as client:
        operator = Operator(client)
        if args.mode == "plan": operator.plan()
        elif args.mode == "reset": operator.reset()
        else: operator.invite(args.email, args.origin.rstrip("/"))

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPError as error:
        print("Network operation failed:", type(error).__name__, "Inspect project state before retrying.", file=sys.stderr)
        sys.exit(1)
