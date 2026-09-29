"""Live operational actions, recalculated from current fleet records on every read."""

from datetime import date
from urllib.parse import urlencode

from .fleet_domain import settings, today

REQUIRED_CERTIFICATIONS = ("Fitness Certificate", "Insurance", "MVL", "Licence")
ALERT_PREFIXES = ("expiry:", "service:", "return:", "assignment-return:", "missing:")


def current_alerts(state):
    result = {}
    day = today()
    current_day = date.fromisoformat(day)
    policy = settings(state)

    def days_until(value):
        return (date.fromisoformat(value) - current_day).days

    def due_text(days):
        unit = "day" if abs(days) == 1 else "days"
        return (
            f"{abs(days)} {unit} overdue"
            if days < 0
            else "due today"
            if days == 0
            else f"due in {days} {unit}"
        )

    def priority_for(days):
        return "overdue" if days < 0 else "due_today" if days == 0 else "upcoming"

    def vehicle_link(vehicle_id):
        return "/admin/fleet?" + urlencode({"vehicleId": vehicle_id})

    def add(key, kind, title, description, link, priority, vehicle, action, **extra):
        existing = state["notifications"].get(key, {})
        # Acknowledging an approaching deadline must not suppress a later escalation.
        same_priority = existing.get("priority") == priority
        result[key] = {
            "id": key,
            "type": kind,
            "title": title,
            "description": description,
            "message": description,
            "link": link,
            "linkTo": link,
            "timestamp": existing.get("timestamp", day + "T00:00:00+04:00"),
            "createdAt": existing.get("createdAt", day + "T00:00:00+04:00"),
            "readBy": existing.get("readBy", []) if same_priority else [],
            "requiresAction": True,
            "priority": priority,
            "vehicleId": vehicle["id"],
            "vehicleReg": vehicle["registrationNumber"],
            "actionLabel": action,
            **extra,
        }

    latest = {}
    for row in state["compliance"].values():
        key = (row["vehicleId"], row["complianceType"])
        if key not in latest or (row["expiryDate"], row.get("createdAt", "")) > (
            latest[key]["expiryDate"],
            latest[key].get("createdAt", ""),
        ):
            latest[key] = row
    for row in latest.values():
        vehicle = state["vehicles"].get(row["vehicleId"])
        if not vehicle:
            continue
        days = days_until(row["expiryDate"])
        if days <= policy.get("complianceNoticeDays", 30):
            status = "expired" if days < 0 else "expires today" if days == 0 else "expiring soon"
            add(
                "expiry:" + row["id"],
                "compliance_expiring",
                f"{row['complianceType']} {status}",
                f"{vehicle['registrationNumber']} · {row['expiryDate']} · {due_text(days)}",
                vehicle_link(vehicle["id"]),
                priority_for(days),
                vehicle,
                "Review / renew certificate",
                dueDate=row["expiryDate"],
                daysRemaining=days,
                complianceType=row["complianceType"],
            )

    for vehicle in state["vehicles"].values():
        missing = []
        for kind in REQUIRED_CERTIFICATIONS:
            certificate = latest.get((vehicle["id"], kind))
            if not certificate:
                missing.append(kind)
            elif not certificate.get("documentUrl"):
                missing.append(kind + " file")
        if missing:
            add(
                "missing:compliance:" + vehicle["id"],
                "records_missing",
                "Compliance records missing",
                vehicle["registrationNumber"] + " · " + ", ".join(missing),
                vehicle_link(vehicle["id"]),
                "missing",
                vehicle,
                "Complete vehicle records",
            )
        target = vehicle.get("nextServiceMileage")
        service_date = vehicle.get("nextServiceDate")
        if target is None and not service_date:
            add(
                "missing:service:" + vehicle["id"],
                "records_missing",
                "Service schedule missing",
                vehicle["registrationNumber"]
                + " · Enter the next service date or mileage to enable reminders.",
                vehicle_link(vehicle["id"]),
                "missing",
                vehicle,
                "Set service schedule",
            )
            continue
        km = target - vehicle["mileage"] if target is not None else None
        days = days_until(service_date) if service_date else None
        date_due = days is not None and days <= policy.get("serviceNoticeDays", 14)
        mileage_due = km is not None and km <= policy.get("serviceNoticeKm", 1500)
        if not (date_due or mileage_due):
            continue
        priority = "upcoming"
        if (days is not None and days < 0) or (km is not None and km < 0):
            priority = "overdue"
        elif days == 0 or km == 0:
            priority = "due_today"
        detail = [vehicle["registrationNumber"]]
        if service_date:
            detail.append(f"Service date {service_date}: {due_text(days)}")
        if km is not None:
            remaining = f"{abs(km):,} km overdue" if km < 0 else f"{km:,} km remaining"
            detail.append(
                f"Odometer {vehicle['mileage']:,} km / service at {target:,} km: {remaining}"
            )
        key = "service:" + ":".join(
            str(vehicle.get(k, "")) for k in ("id", "nextServiceMileage", "nextServiceDate")
        )
        add(
            key,
            "service_due",
            "Service "
            + {"overdue": "overdue", "due_today": "due now", "upcoming": "approaching"}[priority],
            " · ".join(detail),
            vehicle_link(vehicle["id"]),
            priority,
            vehicle,
            "Review / record service",
            dueDate=service_date,
            daysRemaining=days,
            mileageRemaining=km,
        )

    for resource, status_key, active, return_key, prefix, route in [
        ("bookings", "bookingStatus", "active", "returnDate", "return:", "bookings"),
        (
            "assignments",
            "status",
            "Active",
            "expectedReturnDate",
            "assignment-return:",
            "assignments",
        ),
    ]:
        for row in state[resource].values():
            if row[status_key] != active:
                continue
            days = days_until(row[return_key])
            vehicle = state["vehicles"].get(row["vehicleId"])
            if not vehicle or days > policy.get("returnNoticeDays", 1):
                continue
            add(
                prefix + row["id"],
                "vehicle_due_back",
                ("Rental" if resource == "bookings" else "Assignment")
                + " return "
                + due_text(days),
                f"{vehicle['registrationNumber']} · "
                f"{row.get('reference', row.get('assignedTo', ''))} · Expected {row[return_key]}",
                "/admin/"
                + route
                + "?"
                + urlencode({"q": row.get("reference", vehicle["registrationNumber"])}),
                priority_for(days),
                vehicle,
                "Record return",
                dueDate=row[return_key],
                daysRemaining=days,
            )
    return result


def notifications_for(state):
    # Read is an acknowledgement, never a resolution. Only fixing the record removes an action.
    events = {k: v for k, v in state["notifications"].items() if not k.startswith(ALERT_PREFIXES)}
    return {**events, **current_alerts(state)}
