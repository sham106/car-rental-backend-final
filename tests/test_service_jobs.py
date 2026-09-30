from copy import deepcopy

from app.fleet_domain import today
from tests.test_fleet import fleet, post, seed  # noqa: F401


def test_job_snapshot_completion_and_retry(fleet):  # noqa: F811
    client, store, _ = fleet
    vehicle = seed(client)
    job = post(
        client,
        "/admin/records/service_jobs",
        {
            "vehicleId": vehicle["id"],
            "garage": "Workshop",
            "dateOut": today(),
            "expectedReturnDate": today(),
            "deliveredBy": "Driver",
            "requestedWork": "Oil change",
        },
    )
    assert job["status"] == "Pending"
    assert job["reference"].startswith("SVC-")
    assert not store.state["maintenance"]
    assert job["vehicleSnapshot"]["registrationNumber"] == vehicle["registrationNumber"]
    before = deepcopy(store.state)
    payload = {
        "vehicleId": vehicle["id"],
        "garage": "Workshop",
        "date": today(),
        "mileage": 150,
        "serviceType": "Oil Change",
        "description": "Oil replaced",
        "partsCost": 500,
        "labourCost": 100,
        "nextServiceMileage": 10150,
        "mechanic": "Mechanic",
        "collectedBy": "Driver",
        "attachmentIds": ["missing"],
    }
    path = f"/api/admin/service-jobs/{job['id']}/complete"
    assert client.post(path, json=payload).status_code == 404
    assert store.state == before
    payload["attachmentIds"] = []
    result = client.post(path, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "Completed"
    record = next(iter(store.state["maintenance"].values()))
    assert record["serviceJobId"] == job["id"]
    assert record["totalCost"] == 600
    assert store.state["vehicles"][vehicle["id"]]["nextServiceMileage"] == 10150
    assert client.post(path, json=payload).status_code == 200
    assert len(store.state["maintenance"]) == 1
    assert (
        client.patch(
            f"/api/admin/records/service_jobs/{job['id']}", json={"garage": "Changed"}
        ).status_code
        == 405
    )


def test_job_rejects_bad_dates_and_wrong_vehicle_completion(fleet):  # noqa: F811
    client, store, _ = fleet
    vehicle = seed(client)
    payload = {
        "vehicleId": vehicle["id"],
        "garage": "Workshop",
        "dateOut": "2026-02-02",
        "expectedReturnDate": "2026-02-01",
        "deliveredBy": "Driver",
        "requestedWork": "Check brakes",
    }
    assert client.post("/api/admin/records/service_jobs", json=payload).status_code == 422
    assert not store.state["service_jobs"]
    payload["expectedReturnDate"] = "2026-02-03"
    job = post(client, "/admin/records/service_jobs", payload)
    result = client.post(
        f"/api/admin/service-jobs/{job['id']}/complete",
        json={
            "vehicleId": "another-car",
            "garage": "Workshop",
            "date": today(),
            "mileage": 200,
            "serviceType": "Brakes",
            "mechanic": "Mechanic",
            "collectedBy": "Driver",
        },
    )
    assert result.status_code == 422
    assert not store.state["maintenance"]
