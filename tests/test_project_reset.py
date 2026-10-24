import httpx
import pytest
from scripts.reset_test_project import Operator, TABLES


def test_reset_removes_files_before_users_and_checks_empty_state():
    calls = []
    user_deleted = False
    def handle(request):
        nonlocal user_deleted
        path = request.url.path
        calls.append((request.method, path))
        if path.endswith("fleet_snapshot"):
            return httpx.Response(200, json={"revision": 7, "records": {}})
        if path == "/storage/v1/bucket":
            return httpx.Response(200, json=[{"id": "fleet-files"}])
        if path == "/auth/v1/admin/users":
            return httpx.Response(200, json={"users": [] if user_deleted else [{"id": "user-one"}]})
        if request.method == "DELETE" and path == "/auth/v1/admin/users/user-one":
            user_deleted = True
        return httpx.Response(200, json=[])
    with httpx.Client(base_url="https://project.supabase.co", transport=httpx.MockTransport(handle)) as client:
        Operator(client).reset()
    assert calls.index(("POST", "/storage/v1/bucket/fleet-files/empty")) < calls.index(("DELETE", "/auth/v1/admin/users/user-one"))
    assert all(("DELETE", f"/rest/v1/{table}") in calls for table in TABLES)
    assert not any(path.startswith("/auth/v1/invite") for _, path in calls)


def test_failed_storage_cleanup_stops_reset_before_users_are_deleted():
    calls = []
    def handle(request):
        path = request.url.path
        calls.append((request.method, path))
        if path.endswith("fleet_snapshot"): return httpx.Response(200, json={"revision": 1, "records": {}})
        if path == "/storage/v1/bucket": return httpx.Response(200, json=[{"id": "fleet-files"}])
        if path == "/auth/v1/admin/users": return httpx.Response(200, json={"users": [{"id": "old"}]})
        if path.endswith("/empty"): return httpx.Response(500, json={"message": "secret details must not appear"})
        return httpx.Response(200, json=[])
    with httpx.Client(base_url="https://project.supabase.co", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError, match="HTTP 500") as error: Operator(client).reset()
    assert "secret details" not in str(error.value)
    assert not any(method == "DELETE" and path.startswith("/auth/") for method, path in calls)


def test_invitation_uses_live_reset_url_and_grants_membership():
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == "/auth/v1/admin/users": return httpx.Response(200, json={"users": []})
        if request.url.path == "/auth/v1/invite":
            assert request.url.params["redirect_to"] == "https://fleet.example.com/admin/reset-password"
            return httpx.Response(200, json={"id": "new-admin", "email": "shamsikush@gmail.com"})
        if request.method == "GET": return httpx.Response(200, json=[{"role": "super_admin", "is_active": True}])
        return httpx.Response(201)
    with httpx.Client(base_url="https://project.supabase.co", transport=httpx.MockTransport(handle)) as client:
        Operator(client).invite("shamsikush@gmail.com", "https://fleet.example.com")
    assert not any(r.method == "DELETE" for r in calls)
