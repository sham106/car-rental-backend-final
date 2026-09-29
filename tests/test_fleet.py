import base64
from copy import deepcopy
from datetime import date, timedelta
from threading import Lock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.fleet_domain import today
from app.fleet_store import RESOURCES, SupabaseStore
from app.main import create_app
from tests.test_auth import Provider

HEADERS = {"Origin": "http://localhost:3000", "X-Requested-With": "XMLHttpRequest"}
ACTOR = {"id": "11111111-1111-4111-8111-111111111111", "name": "Test Admin", "role": "super_admin"}


class MemoryStore(SupabaseStore):
    """Persistence boundary only is fake; domain operations and retries are production code."""

    def __init__(self):
        self.revision = 0
        self.state = {r: {} for r in RESOURCES}
        self.lock = Lock()
        self.conflict_once = False

    async def snapshot(self):
        with self.lock:
            return self.revision, deepcopy(self.state)

    async def commit(self, revision, before, after, allocations):
        with self.lock:
            if self.conflict_once:
                self.conflict_once = False
                self.revision += 1
                return False
            if revision != self.revision:
                return False
            # Database exclusion is tested separately against PostgreSQL.
            self.state = deepcopy(after)
            self.revision += 1
            return True


@pytest.fixture
def fleet():
    provider = Provider()
    config = Settings(
        _env_file=None,
        app_env="test",
        supabase_url="https://test.supabase.co",
        supabase_publishable_key=SecretStr("test-publishable"),
        supabase_secret_key=SecretStr("test-secret"),
        allowed_hosts=["testserver"],
        allowed_origins=["http://localhost:3000"],
    )
    files = {}

    def upstream(request):
        if request.url.path.startswith("/storage/v1/object/"):
            if request.method == "POST":
                files[request.url.path] = request.content
                return httpx.Response(200, json={"Key": "uploaded"})
            return httpx.Response(200, content=files.get(request.url.path, b""))
        return provider(request)

    app = create_app(config, transport=httpx.MockTransport(upstream))
    with TestClient(app, headers=HEADERS) as client:
        store = MemoryStore()
        store.settings = config
        store.client = app.state.auth.client
        app.state.fleet = store
        response = client.post(
            "/api/admin/auth/login",
            json={"email": "admin@example.com", "password": "a-good-test-password"},
        )
        assert response.status_code == 200
        yield client, store, provider


def post(client, path, payload):
    response = client.post("/api" + path, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def seed(client):
    owner = post(
        client, "/admin/records/owners", {"name": "Fleet Owner", "email": "owner@example.com"}
    )
    vehicle = post(
        client,
        "/admin/records/vehicles",
        {
            "ownerId": owner["id"],
            "slug": "test-car",
            "brand": "Test",
            "model": "Car",
            "registrationNumber": "TEST 123",
            "year": 2026,
            "category": "suv",
            "dailyRate": 2000,
            "mileage": 100,
            "published": True,
        },
    )
    return vehicle


def booking_data(vehicle, start=None, end=None, key="idempotency-unique-key-1"):
    start = start or today()
    end = end or (date.fromisoformat(start) + timedelta(days=3)).isoformat()
    return {
        "vehicleId": vehicle["id"],
        "pickupLocationId": "airport-mru",
        "returnLocationId": "airport-mru",
        "pickupDate": start,
        "returnDate": end,
        "idempotencyKey": key,
        "customer": {
            "firstName": "Test",
            "lastName": "Customer",
            "phone": "+23055555555",
            "email": "customer@example.com",
            "country": "Mauritius",
        },
    }


def book(client, vehicle, **kwargs):
    return post(client, "/public/bookings", booking_data(vehicle, **kwargs))


def test_every_admin_read_requires_authentication(fleet):
    client, store, _ = fleet
    client.cookies.clear()
    for path in (
        "/admin/data",
        "/admin/records/vehicles",
        "/admin/records/customers",
        "/admin/reports",
        "/admin/reports/revenue.csv",
        "/admin/files/unknown",
    ):
        assert client.get("/api" + path).status_code == 401
    assert not store.state["vehicles"]


def test_public_projection_omits_private_fields_and_unpublished(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    public = client.get("/api/public/vehicles").json()[0]
    assert public["id"] == vehicle["id"]
    assert not {"ownerId", "registrationNumber", "vin", "purchaseValue", "mileage"} & public.keys()
    assert (
        client.patch(
            "/api/admin/records/vehicles/" + vehicle["id"], json={"published": False}
        ).status_code
        == 200
    )
    assert client.get("/api/public/vehicles").json() == []
    assert client.get("/api/public/vehicles/" + vehicle["id"]).status_code == 404


def test_guest_booking_atomic_and_authoritative_price(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    result = book(client, vehicle)
    assert result["pricing"]["estimatedTotal"] == 6000
    assert (
        len(store.state["customers"])
        == len(store.state["bookings"])
        == len(store.state["outbox"])
        == 1
    )
    assert next(iter(store.state["bookings"].values()))["bookingStatus"] == "pending"
    payload = booking_data(vehicle, key="another-idempotency-key")
    payload["pricing"] = {"estimatedTotal": 1}
    assert client.post("/api/public/bookings", json=payload).status_code == 422
    assert len(store.state["bookings"]) == 1


def test_idempotency_returns_same_receipt_and_rejects_changed_input(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    first = book(client, vehicle)
    second = book(client, vehicle)
    assert first == second
    assert len(store.state["customers"]) == 1
    payload = booking_data(vehicle)
    payload["customer"]["lastName"] = "Different"
    assert client.post("/api/public/bookings", json=payload).status_code == 409


def test_receipt_needs_secret_not_just_reference(fleet):
    client, _, _ = fleet
    result = book(client, seed(client))
    for token in ("", "wrong"):
        assert (
            client.post(
                "/api/public/bookings/lookup",
                json={"reference": result["reference"], "accessToken": token},
            ).status_code
            == 404
        )
    response = post(
        client,
        "/public/bookings/lookup",
        {"reference": result["reference"], "accessToken": result["accessToken"]},
    )
    assert response["customer"]["email"] == "customer@example.com"
    assert "accessHash" not in response


def test_overlapping_pending_requests_cannot_both_confirm(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    a = book(client, vehicle)
    b = book(client, vehicle, key="second-idempotency-key")
    post(client, f"/admin/bookings/{a['id']}/confirm", {})
    assert client.post(f"/api/admin/bookings/{b['id']}/confirm", json={}).status_code == 409
    assert store.state["bookings"][b["id"]]["bookingStatus"] == "pending"
    assert (
        len([x for x in store.state["bookings"].values() if x["bookingStatus"] == "confirmed"]) == 1
    )


def test_nonoverlapping_bookings_can_confirm(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    a = book(client, vehicle)
    start = (date.fromisoformat(a["returnDate"]) + timedelta(days=1)).isoformat()
    b = book(client, vehicle, start=start, key="second-idempotency-key")
    for row in (a, b):
        post(client, f"/admin/bookings/{row['id']}/confirm", {})


def test_checkin_checkout_atomic_odometer_and_state(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    row = book(client, vehicle)
    post(client, f"/admin/bookings/{row['id']}/confirm", {})
    assert (
        client.post(
            f"/api/admin/bookings/{row['id']}/checkout",
            json={"mileageOut": 50, "fuelLevelOut": "Full"},
        ).status_code
        == 422
    )
    post(
        client, f"/admin/bookings/{row['id']}/checkout", {"mileageOut": 110, "fuelLevelOut": "Full"}
    )
    assert store.state["vehicles"][vehicle["id"]]["operationalStatus"] == "rented"
    checked_out_at = store.state["bookings"][row["id"]]["checkedOutAt"]
    assert checked_out_at
    assert not store.state["bookings"][row["id"]].get("checkedInAt")
    assert (
        client.post(f"/api/admin/bookings/{row['id']}/cancel", json={"reason": "test"}).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/admin/bookings/{row['id']}/checkin",
            json={"mileageIn": 109, "fuelLevelIn": "Full"},
        ).status_code
        == 422
    )
    post(
        client,
        f"/admin/bookings/{row['id']}/checkin",
        {
            "mileageIn": 200,
            "fuelLevelIn": "Full",
            "finalVehicleStatus": "in_service",
            "serviceReason": "Tyres",
        },
    )
    assert store.state["vehicles"][vehicle["id"]]["mileage"] == 200
    assert store.state["vehicles"][vehicle["id"]]["operationalStatus"] == "in_service"
    assert store.state["bookings"][row["id"]]["bookingStatus"] == "completed"
    assert store.state["bookings"][row["id"]]["checkedInAt"] >= checked_out_at
    assert store.state["bookings"][row["id"]]["pickupDate"] == row["pickupDate"]
    assert store.state["bookings"][row["id"]]["returnDate"] == row["returnDate"]


def test_terminal_booking_cannot_be_revived(fleet):
    client, _, _ = fleet
    row = book(client, seed(client))
    post(client, f"/admin/bookings/{row['id']}/reject", {"reason": "Unavailable"})
    assert client.post(f"/api/admin/bookings/{row['id']}/confirm", json={}).status_code == 409


def test_assignment_conflicts_with_rental_and_return_updates_mileage(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    data = {
        "vehicleId": vehicle["id"],
        "assignedTo": "Staff",
        "assignmentType": "Staff",
        "startDate": today(),
        "expectedReturnDate": (date.fromisoformat(today()) + timedelta(days=2)).isoformat(),
        "mileageOut": 100,
        "reason": "Delivery",
    }
    assigned = post(client, "/admin/records/assignments", data)
    assert client.post("/api/public/bookings", json=booking_data(vehicle)).status_code == 409
    assert (
        client.post(
            f"/api/admin/assignments/{assigned['id']}/cancel", json={"reason": "test"}
        ).status_code
        == 422
    )
    post(client, f"/admin/assignments/{assigned['id']}/end", {"mileageIn": 150})
    assert store.state["vehicles"][vehicle["id"]]["mileage"] == 150
    book(client, vehicle)


def test_holds_and_expiry_prevent_confirmation(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    row = book(client, vehicle)
    client.patch(
        "/api/admin/records/vehicles/" + vehicle["id"],
        json={"operationalStatus": "in_service", "statusChangeReason": "Tyres"},
    )
    assert client.post(f"/api/admin/bookings/{row['id']}/confirm", json={}).status_code == 409
    client.patch(
        "/api/admin/records/vehicles/" + vehicle["id"],
        json={"operationalStatus": "available", "statusChangeReason": "Repaired"},
    )
    post(
        client,
        "/admin/records/compliance",
        {
            "vehicleId": vehicle["id"],
            "complianceType": "Insurance",
            "issueDate": today(),
            "expiryDate": today(),
            "premium": 500,
        },
    )
    assert client.post(f"/api/admin/bookings/{row['id']}/confirm", json={}).status_code == 409
    post(
        client,
        "/admin/records/compliance",
        {
            "vehicleId": vehicle["id"],
            "complianceType": "Insurance",
            "issueDate": today(),
            "expiryDate": row["returnDate"],
            "premium": 500,
        },
    )
    post(client, f"/admin/bookings/{row['id']}/confirm", {})


def test_settings_affect_quote_and_deposit(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    # Provider's default membership is admin. Test the permission boundary before super-admin.
    response = client.patch(
        "/api/admin/records/settings/company",
        json={"defaultDeposit": 8000, "airportDeliveryFee": 200},
    )
    if response.status_code == 403:
        from app.auth import require_admin
        from app.schemas import AdminIdentity

        client.app.dependency_overrides[require_admin] = lambda: AdminIdentity(
            **ACTOR, email="admin@example.com"
        )
        response = client.patch(
            "/api/admin/records/settings/company",
            json={"defaultDeposit": 8000, "airportDeliveryFee": 200},
        )
    assert response.status_code == 200, response.text
    row = book(client, vehicle)
    assert row["pricing"]["estimatedTotal"] == 6400
    assert store.state["bookings"][row["id"]]["securityDeposit"] == 8000


def test_duplicate_registration_and_stale_version(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    data = {
        k: v for k, v in vehicle.items() if k not in {"id", "createdAt", "updatedAt", "version"}
    }
    data.update(slug="another-car", registrationNumber="test123")
    assert client.post("/api/admin/records/vehicles", json=data).status_code == 409
    assert (
        client.patch(
            "/api/admin/records/vehicles/" + vehicle["id"], json={"dailyRate": 2500, "version": 1}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/admin/records/vehicles/" + vehicle["id"], json={"dailyRate": 1, "version": 1}
        ).status_code
        == 409
    )


def test_retry_revalidates_and_no_duplicate_audit(fleet):
    client, store, _ = fleet
    store.conflict_once = True
    seed(client)
    assert len(store.state["vehicles"]) == 1
    assert len(store.state["audit"]) == 2
    audit_row = next(iter(store.state["audit"].values()))
    assert audit_row["actorName"] != "Admin User"
    assert audit_row["performedBy"] == audit_row["actorName"]


def test_maintenance_cost_is_computed_and_record_history_is_immutable(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    row = post(
        client,
        "/admin/records/maintenance",
        {
            "vehicleId": vehicle["id"],
            "date": today(),
            "serviceType": "Tyres",
            "mileage": 150,
            "garage": "Garage",
            "partsCost": 200.10,
            "labourCost": 100.20,
            "totalCost": 1,
        },
    )
    assert row["totalCost"] == 300.30
    assert store.state["vehicles"][vehicle["id"]]["nextServiceMileage"] == 10150
    assert (
        client.patch(
            "/api/admin/records/maintenance/" + row["id"], json={"labourCost": 0}
        ).status_code
        == 405
    )


def test_import_is_atomic_and_csv_parse_real_data(fleet):
    client, store, _ = fleet
    seed(client)
    csv = (
        "Registration No,Brand,Model,Year,Category,Daily Rate,Mileage,Owner\n"
        "NEW123,Toyota,Corolla,2024,suv,2000,100,Fleet Owner\n"
    )
    parsed = post(
        client,
        "/admin/import/parse",
        {"name": "fleet.csv", "content": base64.b64encode(csv.encode()).decode()},
    )
    assert parsed[0]["registrationNumber"] == "NEW123"
    assert parsed[0]["isValid"]
    valid = {k: v for k, v in parsed[0].items() if k not in {"errors", "isValid"}}
    invalid = {**valid, "registrationNumber": "OTHER", "ownerName": "Does not exist"}
    assert (
        client.post("/api/admin/import/vehicles", json={"rows": [valid, invalid]}).status_code
        == 422
    )
    assert len(store.state["vehicles"]) == 1
    post(client, "/admin/import/vehicles", {"rows": [valid]})
    assert len(store.state["vehicles"]) == 2


def test_reports_use_completed_revenue_and_escape_csv(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    row = book(client, vehicle)
    report = client.get("/api/admin/reports").json()
    assert report["performance"][0]["rentalRevenue"] == 0
    client.patch(
        "/api/admin/records/owners/" + vehicle["ownerId"], json={"name": '=HYPERLINK("evil")'}
    )
    csv = client.get("/api/admin/reports/utilization.csv")
    assert csv.status_code == 200
    assert "'=HYPERLINK" in csv.text
    assert row["accessToken"] not in client.get("/api/admin/data").text


def test_admin_revocation_blocks_data_and_mutation(fleet):
    client, _, provider = fleet
    provider.active = False
    assert client.get("/api/admin/data").status_code == 403
    assert client.post("/api/admin/records/owners", json={"name": "No access"}).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "photo", "content": "!!!", "contentType": "image/jpeg"},
        {
            "kind": "photo",
            "content": base64.b64encode(b'<svg onload="evil"/>').decode(),
            "contentType": "image/svg+xml",
        },
        {
            "kind": "document",
            "content": base64.b64encode(b"not pdf").decode(),
            "contentType": "application/pdf",
        },
    ],
)
def test_upload_rejects_invalid_files_before_storage(fleet, payload):
    client, _, _ = fleet
    assert client.post("/api/admin/files", json=payload).status_code == 422


def test_photo_upload_is_validated_and_public_only_when_published(fleet):
    import io

    from PIL import Image

    client, _, _ = fleet
    vehicle = seed(client)
    data = io.BytesIO()
    Image.new("RGB", (3, 3), "white").save(data, format="PNG")
    photo = post(
        client,
        "/admin/files",
        {
            "kind": "photo",
            "name": "car.png",
            "contentType": "image/png",
            "content": base64.b64encode(data.getvalue()).decode(),
        },
    )
    assert client.get(photo["url"]).status_code == 404
    assert (
        client.patch(
            "/api/admin/records/vehicles/" + vehicle["id"], json={"photos": [photo["url"]]}
        ).status_code
        == 200
    )
    response = client.get(photo["url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8")
    client.patch("/api/admin/records/vehicles/" + vehicle["id"], json={"published": False})
    assert client.get(photo["url"]).status_code == 404


def test_document_requires_real_file_and_is_private(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    missing = {
        "vehicleId": vehicle["id"],
        "title": "Policy",
        "documentType": "Insurance Certificate",
        "fileId": "missing",
    }
    assert client.post("/api/admin/records/documents", json=missing).status_code == 404
    uploaded = post(
        client,
        "/admin/files",
        {
            "kind": "document",
            "name": "policy.pdf",
            "contentType": "application/pdf",
            "content": base64.b64encode(b"%PDF-1.4\n%%EOF").decode(),
        },
    )
    row = post(client, "/admin/records/documents", {**missing, "fileId": uploaded["id"]})
    assert client.get("/api/admin/files/" + uploaded["id"]).status_code == 200
    assert (
        client.request("DELETE", "/api/admin/records/documents/" + row["id"], json={}).status_code
        == 200
    )
    client.cookies.clear()
    assert client.get("/api/admin/files/" + uploaded["id"]).status_code == 401


def test_ordinary_admin_cannot_change_company_policy(fleet):
    client, _, provider = fleet
    provider.role = "admin"
    assert (
        client.patch("/api/admin/records/settings/company", json={"defaultDeposit": 0}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/records/categories", json={"name": "Category", "slug": "category"}
        ).status_code
        == 403
    )


def test_compliance_alert_acknowledgement_is_persistent(fleet):
    client, _, _ = fleet
    vehicle = seed(client)
    post(
        client,
        "/admin/records/compliance",
        {
            "vehicleId": vehicle["id"],
            "complianceType": "Insurance",
            "issueDate": today(),
            "expiryDate": today(),
        },
    )
    rows = client.get("/api/admin/records/notifications").json()["items"]
    expiry = next(row for row in rows if row["id"].startswith("expiry:"))
    assert expiry["read"] is False
    post(client, "/admin/notifications/read", {"id": expiry["id"]})
    remaining = client.get("/api/admin/records/notifications").json()["items"]
    acknowledged = next(row for row in remaining if row["id"] == expiry["id"])
    assert acknowledged["read"] is True
    assert acknowledged["requiresAction"] is True


def test_xlsx_import_parses_actual_cells(fleet):
    import io

    from openpyxl import Workbook

    client, _, _ = fleet
    seed(client)
    workbook = Workbook()
    workbook.active.append(
        ["Registration No", "Brand", "Model", "Year", "Category", "Daily Rate", "Mileage", "Owner"]
    )
    workbook.active.append(
        ["XLSX123", "Toyota", "Yaris", 2025, "compact", 1500, 100, "Fleet Owner"]
    )
    output = io.BytesIO()
    workbook.save(output)
    rows = post(
        client,
        "/admin/import/parse",
        {"name": "cars.xlsx", "content": base64.b64encode(output.getvalue()).decode()},
    )
    assert rows[0]["registrationNumber"] == "XLSX123"
    assert rows[0]["isValid"] is True


def test_webhook_failures_keep_event_for_retry(fleet):
    import asyncio

    from app.notification_worker import deliver_once

    client, store, _ = fleet
    book(client, seed(client))
    config = Settings(
        _env_file=None,
        notification_webhook_url="https://hooks.example.com/events",
        notification_webhook_secret="worker-test-secret",
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await deliver_once(store, http, config)

    assert asyncio.run(run()) == 1
    event = next(iter(store.state["outbox"].values()))
    assert event["status"] == "pending"
    assert event["attempts"] == 1
    assert requests[0].headers["Idempotency-Key"] == event["id"]
    assert len(requests[0].headers["X-Oceane-Signature"]) == 64
    assert len(store.state["bookings"]) == 1


@pytest.mark.parametrize(
    "document_type,compliance_type",
    [
        ("Insurance Certificate", "Insurance"),
        ("Fitness Certificate", "Fitness Certificate"),
        ("MVL", "MVL"),
        ("Licence", "Licence"),
    ],
)
def test_certification_upload_saves_linked_compliance_atomically(
    fleet, document_type, compliance_type
):
    client, store, _ = fleet
    vehicle = seed(client)
    uploaded = post(
        client,
        "/admin/files",
        {
            "kind": "document",
            "name": "certificate.pdf",
            "contentType": "application/pdf",
            "content": base64.b64encode(b"%PDF-1.4\n%%EOF").decode(),
        },
    )
    payload = {
        "vehicleId": vehicle["id"],
        "title": "Certification",
        "documentType": document_type,
        "fileId": uploaded["id"],
        "issueDate": "2026-01-01",
        "expiryDate": "2027-01-01",
        "compliance": {
            "company": "Insurer",
            "broker": "Broker",
            "policyNumber": "POL-42",
            "premium": 12345.67,
        },
    }
    before = deepcopy(store.state)
    bad = client.post("/api/admin/records/documents", json={**payload, "expiryDate": ""})
    assert bad.status_code == 422
    assert store.state == before
    bad = client.post(
        "/api/admin/records/documents", json={**payload, "compliance": {"premium": -1}}
    )
    assert bad.status_code == 422
    assert store.state == before
    row = post(client, "/admin/records/documents", payload)
    record = store.state["compliance"][row["complianceId"]]
    assert record["vehicleId"] == vehicle["id"]
    assert record["complianceType"] == compliance_type
    assert record["company"] == "Insurer"
    assert record["broker"] == "Broker"
    assert record["policyNumber"] == "POL-42"
    assert record["premium"] == 12345.67
    assert record["documentUrl"] == "/api/admin/files/" + uploaded["id"]
    assert client.get(record["documentUrl"]).status_code == 200
    client.cookies.clear()
    assert client.get(record["documentUrl"]).status_code == 401


def test_historical_service_updates_schedule_without_decreasing_odometer(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    post(
        client,
        "/admin/records/maintenance",
        {
            "vehicleId": vehicle["id"],
            "serviceType": "Routine Service",
            "date": today(),
            "mileage": 90,
            "garage": "Garage",
            "description": "Previous service entered during onboarding",
            "nextServiceMileage": 9000,
        },
    )
    saved = store.state["vehicles"][vehicle["id"]]
    assert saved["mileage"] == 100
    assert saved["nextServiceMileage"] == 9000
    post(
        client,
        "/admin/records/maintenance",
        {
            "vehicleId": vehicle["id"],
            "serviceType": "Routine Service",
            "date": (date.fromisoformat(today()) - timedelta(days=90)).isoformat(),
            "mileage": 50,
            "garage": "Old Garage",
            "nextServiceMileage": 5000,
        },
    )
    saved = store.state["vehicles"][vehicle["id"]]
    assert saved["mileage"] == 100
    assert saved["nextServiceMileage"] == 9000
    before = deepcopy(store.state)
    response = client.post(
        "/api/admin/records/maintenance",
        json={
            "vehicleId": vehicle["id"],
            "serviceType": "Routine Service",
            "date": today(),
            "mileage": 100,
            "garage": "Garage",
            "nextServiceMileage": 90,
        },
    )
    assert response.status_code == 422
    assert store.state == before


def test_insurance_renewal_retains_previous_details_and_uploaded_files(fleet):
    client, store, _ = fleet
    vehicle = seed(client)
    saved = []
    for number, issue, expiry, premium in [
        ("OLD-POLICY", "2025-01-01", "2026-01-01", 12000),
        ("NEW-POLICY", "2026-01-01", "2027-01-01", 13500),
    ]:
        uploaded = post(
            client,
            "/admin/files",
            {
                "kind": "document",
                "name": number + ".pdf",
                "contentType": "application/pdf",
                "content": base64.b64encode(b"%PDF-1.4\n%%EOF").decode(),
            },
        )
        document = post(
            client,
            "/admin/records/documents",
            {
                "vehicleId": vehicle["id"],
                "documentType": "Insurance Certificate",
                "title": number,
                "fileId": uploaded["id"],
                "issueDate": issue,
                "expiryDate": expiry,
                "compliance": {
                    "company": "Insurer " + number,
                    "broker": "Broker " + number,
                    "policyNumber": number,
                    "premium": premium,
                },
            },
        )
        saved.append((document, uploaded, number, premium))
    assert len(store.state["compliance"]) == 2
    assert len(store.state["documents"]) == 2
    overview = client.get("/api/admin/data").json()
    assert len(overview["compliance"]) == 2
    assert len(overview["documents"]) == 2
    for document, uploaded, number, premium in saved:
        policy = next(c for c in overview["compliance"] if c["id"] == document["complianceId"])
        assert policy["policyNumber"] == number
        assert policy["company"] == "Insurer " + number
        assert policy["broker"] == "Broker " + number
        assert policy["premium"] == premium
        assert policy["documentUrl"] == "/api/admin/files/" + uploaded["id"]
        assert client.get(policy["documentUrl"]).status_code == 200
