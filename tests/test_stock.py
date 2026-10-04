from uuid import uuid4
import pytest
from tests.test_fleet import fleet, post


def item(client, **extra):
    return post(client, "/admin/stock/items", {"name": "Engine oil", "sku": "OIL-1", "unit": "litres", "requestId": str(uuid4()), **extra})


def movement(client, key, direction, quantity, request_id=None):
    return client.post(f"/api/admin/stock/items/{key}/movements", json={"direction": direction, "quantity": quantity, "reason": "Workshop", "requestId": request_id or str(uuid4())})


def test_stock_lifecycle_and_duplicate_movement(fleet):
    client, store, _ = fleet
    row = item(client, lowStockThreshold=3)
    key = row["id"]
    request_id = str(uuid4())
    first = movement(client, key, "in", 10.5, request_id)
    assert first.status_code == 200
    assert movement(client, key, "in", 10.5, request_id).json()["id"] == first.json()["id"]
    assert movement(client, key, "out", 8).status_code == 200
    summary = client.get("/api/admin/stock").json()
    assert len(summary["movements"]) == 2
    assert summary["items"][0]["remaining"] == 2.5
    assert summary["items"][0]["issued"] == 8
    assert summary["items"][0]["status"] == "Low stock"
    assert movement(client, key, "out", 3).status_code == 409
    assert movement(client, key, "out", 2.5).status_code == 200
    assert client.get("/api/admin/stock").json()["items"][0]["status"] == "Out of stock"


@pytest.mark.parametrize("quantity", [0, -1, 0.0001, "nan", 1000000001])
def test_invalid_quantities_rejected(fleet, quantity):
    client, _, _ = fleet
    row = item(client)
    assert movement(client, row["id"], "in", quantity).status_code == 422


def test_create_idempotency_sku_and_stale_edits(fleet):
    client, _, _ = fleet
    request_id = str(uuid4())
    row = item(client, requestId=request_id)
    assert item(client, requestId=request_id)["id"] == row["id"]
    assert client.post("/api/admin/stock/items", json={"name": "Other", "sku": "oil-1", "requestId": str(uuid4())}).status_code == 409
    payload = {"name": "Updated", "sku": "OIL-1", "unit": "litres", "version": row["version"], "requestId": str(uuid4())}
    movement(client, row["id"], "in", 1)
    assert client.patch(f"/api/admin/stock/items/{row['id']}", json=payload).status_code == 409
    payload["version"] += 1
    payload["active"] = False
    assert client.patch(f"/api/admin/stock/items/{row['id']}", json=payload).status_code == 200
    assert movement(client, row["id"], "in", 1).status_code == 422


def test_retry_revalidates_balance_after_concurrent_withdrawal(fleet):
    client, store, _ = fleet
    row = item(client)
    key = row["id"]
    movement(client, key, "in", 5)
    original_commit = store.commit
    first = True
    async def concurrent_commit(revision, before, after, allocations):
        nonlocal first
        if first:
            first = False
            from app.stock import move_stock
            move_stock(store.state, {"id": "other", "name": "Other admin", "role": "admin"}, key,
                       {"direction": "out", "quantity": 4, "reason": "Concurrent issue", "requestId": str(uuid4())})
            store.revision += 1
            return False
        return await original_commit(revision, before, after, allocations)
    store.commit = concurrent_commit
    assert movement(client, key, "out", 3).status_code == 409
    assert client.get("/api/admin/stock").json()["items"][0]["remaining"] == 1


def test_stock_requires_authentication_and_no_direct_ledger_edit(fleet):
    client, _, _ = fleet
    assert client.post("/api/admin/records/stock_movements", json={}).status_code == 405
    client.cookies.clear()
    assert client.get("/api/admin/stock").status_code == 401
    assert client.post("/api/admin/stock/items", json={}).status_code == 401
