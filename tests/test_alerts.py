from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.fleet_alerts import current_alerts, notifications_for
from app.fleet_domain import defaults, read_rows
from app.fleet_store import RESOURCES

DAY = date(2026, 9, 29)


def offset(days):
    return (DAY + timedelta(days=days)).isoformat()


@pytest.fixture
def alert_state(monkeypatch):
    monkeypatch.setattr("app.fleet_alerts.today", lambda: DAY.isoformat())
    state = {name: {} for name in RESOURCES}
    defaults(state)
    state["vehicles"]["car"] = {
        "id": "car",
        "registrationNumber": "AB & 123",
        "mileage": 10000,
        "nextServiceMileage": 20000,
        "nextServiceDate": offset(100),
    }
    for kind in ["Fitness Certificate", "Insurance", "MVL", "Licence"]:
        state["compliance"][kind] = {
            "id": kind,
            "vehicleId": "car",
            "vehicleReg": "AB & 123",
            "documentUrl": "/api/admin/files/certificate",
            "complianceType": kind,
            "expiryDate": offset(100),
            "createdAt": offset(-200),
        }
    return state


@pytest.mark.parametrize("kind", ["Fitness Certificate", "Insurance", "MVL", "Licence"])
@pytest.mark.parametrize(
    "days,priority", [(-1, "overdue"), (0, "due_today"), (30, "upcoming"), (31, None)]
)
def test_each_certificate_alert_boundary(alert_state, kind, days, priority):
    alert_state["compliance"][kind]["expiryDate"] = offset(days)
    alerts = current_alerts(alert_state)
    if priority is None:
        assert not alerts
    else:
        alert = alerts["expiry:" + kind]
        assert alert["priority"] == priority
        assert alert["daysRemaining"] == days
        assert alert["vehicleId"] == "car"
        assert alert["requiresAction"] is True
        assert alert["link"] == "/admin/fleet?vehicleId=car"


@pytest.mark.parametrize(
    "days,km,priority",
    [
        (14, 10000, "upcoming"),
        (15, 10000, None),
        (100, 1500, "upcoming"),
        (100, 1501, None),
        (0, 10000, "due_today"),
        (100, 0, "due_today"),
        (-1, 10000, "overdue"),
        (100, -1, "overdue"),
        (5, -100, "overdue"),
    ],
)
def test_service_date_or_mileage_triggers_and_uses_most_urgent_condition(
    alert_state, days, km, priority
):
    vehicle = alert_state["vehicles"]["car"]
    vehicle.update(nextServiceDate=offset(days), nextServiceMileage=vehicle["mileage"] + km)
    alerts = list(current_alerts(alert_state).values())
    if priority is None:
        assert not alerts
    else:
        assert len(alerts) == 1
        assert alerts[0]["priority"] == priority
        assert alerts[0]["daysRemaining"] == days
        assert alerts[0]["mileageRemaining"] == km
        assert "Odometer 10,000 km" in alerts[0]["description"]


def test_seen_alert_escalates_again_and_refreshes_its_live_details(alert_state):
    vehicle = alert_state["vehicles"]["car"]
    vehicle["nextServiceMileage"] = 10500
    alert = next(iter(current_alerts(alert_state).values()))
    alert["readBy"] = ["admin"]
    alert_state["notifications"][alert["id"]] = alert
    assert read_rows(alert_state, "notifications", {"id": "admin"})[0]["read"] is True
    vehicle["mileage"] = 10600
    escalated = next(iter(current_alerts(alert_state).values()))
    assert escalated["priority"] == "overdue"
    assert escalated["readBy"] == []
    assert escalated["requiresAction"] is True
    assert "100 km overdue" in escalated["description"]
    assert "10,600 km" in escalated["description"]


def test_renewal_and_new_service_schedule_resolve_even_acknowledged_alerts(alert_state):
    old = alert_state["compliance"]["Insurance"]
    old["expiryDate"] = offset(-1)
    alert_state["vehicles"]["car"]["nextServiceDate"] = offset(-2)
    alert_state["notifications"].update(deepcopy(current_alerts(alert_state)))
    alert_state["compliance"]["renewal"] = {**old, "id": "renewal", "expiryDate": offset(365)}
    alert_state["vehicles"]["car"]["nextServiceDate"] = offset(100)
    assert notifications_for(alert_state) == {}
    assert alert_state["compliance"]["Insurance"]["expiryDate"] == offset(-1)


def test_missing_records_do_not_report_fleet_as_clear(alert_state):
    del alert_state["compliance"]["Licence"]
    vehicle = alert_state["vehicles"]["car"]
    vehicle["nextServiceMileage"] = None
    vehicle["nextServiceDate"] = None
    alerts = current_alerts(alert_state)
    assert len(alerts) == 2
    assert all(a["priority"] == "missing" for a in alerts.values())
    assert "Licence" in alerts["missing:compliance:car"]["description"]


@pytest.mark.parametrize(
    "resource,status_key,status,return_key,prefix",
    [
        ("bookings", "bookingStatus", "active", "returnDate", "return:"),
        ("assignments", "status", "Active", "expectedReturnDate", "assignment-return:"),
    ],
)
def test_active_returns_approach_escalate_and_resolve(
    alert_state, resource, status_key, status, return_key, prefix
):
    row = {
        "id": "trip",
        "vehicleId": "car",
        status_key: status,
        return_key: offset(1),
        "assignedTo": "Custodian",
    }
    alert_state[resource]["trip"] = row
    assert current_alerts(alert_state)[prefix + "trip"]["priority"] == "upcoming"
    row[return_key] = offset(0)
    assert current_alerts(alert_state)[prefix + "trip"]["priority"] == "due_today"
    row[return_key] = offset(-1)
    assert current_alerts(alert_state)[prefix + "trip"]["priority"] == "overdue"
    assert "q=AB+%26+123" in current_alerts(alert_state)[prefix + "trip"]["link"]
    row[status_key] = "completed" if resource == "bookings" else "Completed"
    assert not current_alerts(alert_state)


def test_custom_windows_and_legacy_settings_defaults(alert_state):
    alert_state["settings"]["company"].update(
        serviceNoticeDays=2, serviceNoticeKm=100, complianceNoticeDays=5
    )
    alert_state["vehicles"]["car"].update(nextServiceDate=offset(3), nextServiceMileage=10101)
    alert_state["compliance"]["Insurance"]["expiryDate"] = offset(6)
    assert not current_alerts(alert_state)
    alert_state["vehicles"]["car"]["nextServiceMileage"] = 10100
    assert len(current_alerts(alert_state)) == 1
    for key in ["serviceNoticeDays", "serviceNoticeKm", "returnNoticeDays"]:
        alert_state["settings"]["company"].pop(key, None)
    assert len(current_alerts(alert_state)) == 1
