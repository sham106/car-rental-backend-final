from datetime import date

from .fleet_domain import settings, today


def current_alerts(state):
    result = {}
    day = today()

    def add(key, kind, title, description, link):
        existing = state["notifications"].get(key)
        result[key] = existing or {
            "id": key,
            "type": kind,
            "title": title,
            "description": description,
            "message": description,
            "link": link,
            "linkTo": link,
            "timestamp": day + "T00:00:00+04:00",
            "createdAt": day + "T00:00:00+04:00",
            "readBy": [],
        }

    latest = {}
    for row in state["compliance"].values():
        key = (row["vehicleId"], row["complianceType"])
        if key not in latest or row["expiryDate"] > latest[key]["expiryDate"]:
            latest[key] = row
    for row in latest.values():
        days = (date.fromisoformat(row["expiryDate"]) - date.fromisoformat(day)).days
        if days <= settings(state)["complianceNoticeDays"]:
            add(
                "expiry:" + row["id"],
                "insurance_expiring",
                f"{row['complianceType']} {'expired' if days < 0 else 'expiring'}",
                f"{row['vehicleReg']} · {row['expiryDate']}",
                "/admin/compliance",
            )
    for row in state["vehicles"].values():
        if (
            row.get("nextServiceMileage") is not None
            and row["mileage"] >= row["nextServiceMileage"]
        ) or (row.get("nextServiceDate") and row["nextServiceDate"] <= day):
            add(
                "service:"
                + ":".join(
                    str(row.get(k, "")) for k in ("id", "nextServiceMileage", "nextServiceDate")
                ),
                "service_overdue",
                "Vehicle service due",
                row["registrationNumber"],
                "/admin/maintenance",
            )
    for row in state["bookings"].values():
        if row["bookingStatus"] == "active" and row["returnDate"] <= day:
            add(
                "return:" + row["id"],
                "vehicle_due_back",
                "Vehicle due back",
                row["reference"],
                "/admin/bookings",
            )
    return result


def notifications_for(state):
    # Resolved alerts disappear; event notifications remain until acknowledged.
    events = {
        k: v
        for k, v in state["notifications"].items()
        if not k.startswith(("expiry:", "service:", "return:"))
    }
    return {**events, **current_alerts(state)}
