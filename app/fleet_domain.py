"""Business operations run against a consistent snapshot, then commit atomically."""

import hashlib
import hmac
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from .fleet_models import MODELS, BookingInput, CheckinInput, CheckoutInput, SettingsInput

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text())
HOLDS = {"in_service", "compliance_hold", "inactive"}
PUBLIC_VEHICLE_FIELDS = {
    "id",
    "slug",
    "brand",
    "model",
    "year",
    "color",
    "category",
    "description",
    "dailyRate",
    "transmission",
    "fuelType",
    "seats",
    "luggageCapacity",
    "doors",
    "airConditioning",
    "features",
    "photos",
    "operationalStatus",
    "published",
    "featured",
}
DERIVED = {
    "id",
    "createdAt",
    "updatedAt",
    "ownerName",
    "vehicleReg",
    "vehicleName",
    "vehicleCount",
    "totalRentals",
    "totalBookings",
    "totalSpend",
    "name",
    "fullName",
    "licenseNumber",
    "status",
    "totalCost",
    "uploadedAt",
    "fileUrl",
    "fileSize",
    "currentBookingId",
    "currentAssignmentId",
    "statusChangedAt",
    "version",
}


def now():
    return datetime.now(UTC).isoformat()


def today():
    # Mauritius is UTC+4 year-round, also works on Windows without system tzdata.
    return (datetime.now(UTC) + timedelta(hours=4)).date().isoformat()


def money(value):
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def fail(message, status=422):
    raise HTTPException(status, message)


def find(state, resource, key):
    value = state[resource].get(key)
    if value is None:
        fail(f"{resource.title()} record not found.", 404)
    return value


def validate(model, payload):
    try:
        return model.model_validate(payload).model_dump(mode="json", exclude_none=True)
    except ValidationError as error:
        fields = ", ".join(".".join(str(x) for x in e["loc"]) for e in error.errors()[:5])
        fail(f"Check the following fields: {fields}.")


def defaults(state):
    for resource, values in CATALOG.items():
        # Catalog is initialized explicitly on first write; never resurrect deleted records.
        if not state["settings"]:
            for value in values:
                state[resource][value["id"]] = deepcopy(value)
    if not state["settings"]:
        state["settings"]["company"] = {
            "id": "company",
            **SettingsInput().model_dump(exclude_none=True),
        }


def settings(state):
    return state["settings"].get("company", SettingsInput().model_dump(exclude_none=True))


def new_record(state, resource, data, key=None):
    key = key or str(uuid4())
    row = {**data, "id": key, "createdAt": now(), "updatedAt": now(), "version": 1}
    state[resource][key] = row
    return row


def touch(row):
    row["updatedAt"] = now()
    row["version"] = row.get("version", 0) + 1


def audit(state, actor, action, resource, row, reason=""):
    name = actor["name"] if actor else "Website guest"
    new_record(
        state,
        "audit",
        {
            "timestamp": now(),
            "actorId": actor["id"] if actor else None,
            "actorName": name,
            "performedBy": name,
            "actorRole": actor["role"] if actor else "guest",
            "action": action,
            "targetType": {"maintenance": "Maintenance", "compliance": "Compliance"}.get(
                resource, resource.rstrip("s").title()
            ),
            "targetEntity": resource,
            "targetId": row["id"],
            "targetEntityId": row["id"],
            "targetLabel": row.get(
                "reference", row.get("registrationNumber", row.get("name", resource))
            ),
            "details": f"{action}: {resource}",
            "reason": reason,
        },
    )


def notification(state, kind, title, description, link="/admin/bookings"):
    row = new_record(
        state,
        "notifications",
        {
            "type": kind,
            "title": title,
            "description": description,
            "message": description,
            "timestamp": now(),
            "readBy": [],
            "link": link,
            "linkTo": link,
        },
    )
    # External delivery is a durable, separately retried task; failures never lose a booking.
    new_record(
        state,
        "outbox",
        {
            "event": kind,
            "notificationId": row["id"],
            "payload": {"title": title, "description": description, "link": link},
            "status": "pending",
            "attempts": 0,
            "nextAttemptAt": now(),
        },
    )


def allocations(state):
    result = []
    for row in state["bookings"].values():
        if row["bookingStatus"] in {"confirmed", "active"}:
            result.append(
                {
                    "id": row["id"],
                    "vehicleId": row["vehicleId"],
                    "startDate": row["pickupDate"],
                    "endDate": row["returnDate"],
                    "kind": "booking",
                }
            )
    for row in state["assignments"].values():
        if row["status"] == "Active":
            result.append(
                {
                    "id": row["id"],
                    "vehicleId": row["vehicleId"],
                    "startDate": row["startDate"],
                    "endDate": row["expectedReturnDate"],
                    "kind": "assignment",
                }
            )
    return result


def dates(start, end, allow_past=False):
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        fail("Enter valid pickup and return dates.")
    if b < a or (b - a).days > 730:
        fail("Return must be on or after pickup, within two years.")
    if not allow_past and start < today():
        fail("Pickup cannot be in the past.")
    return max(1, (b - a).days)


def unavailable(state, vehicle, start, end, exclude=None):
    if vehicle["operationalStatus"] in HOLDS:
        return (
            "maintenance_block"
            if vehicle["operationalStatus"] == "in_service"
            else "administrative_hold"
        )
    # An overdue physical checkout must be returned before accepting another handover.
    for row in state["bookings"].values():
        if (
            row["id"] != exclude
            and row["vehicleId"] == vehicle["id"]
            and row["bookingStatus"] == "active"
            and row["returnDate"] < today()
        ):
            return "active_rental"
    for row in state["assignments"].values():
        if (
            row["id"] != exclude
            and row["vehicleId"] == vehicle["id"]
            and row["status"] == "Active"
            and row["expectedReturnDate"] < today()
        ):
            return "administrative_hold"
    # Latest renewal for each document type is authoritative; older expired history remains.
    latest = {}
    for row in state["compliance"].values():
        if row["vehicleId"] == vehicle["id"]:
            latest[row["complianceType"]] = max(
                latest.get(row["complianceType"], ""), row["expiryDate"]
            )
    if any(expiry < end for expiry in latest.values()):
        return "administrative_hold"
    for block in allocations(state):
        if (
            block["id"] != exclude
            and block["vehicleId"] == vehicle["id"]
            and start <= block["endDate"]
            and end >= block["startDate"]
        ):
            return "confirmed_booking" if block["kind"] == "booking" else "administrative_hold"
    return None


def refresh_vehicle(state, vehicle):
    active = next(
        (
            x
            for x in state["bookings"].values()
            if x["vehicleId"] == vehicle["id"] and x["bookingStatus"] == "active"
        ),
        None,
    )
    assigned = next(
        (
            x
            for x in state["assignments"].values()
            if x["vehicleId"] == vehicle["id"]
            and x["status"] == "Active"
            and x["startDate"] <= today()
        ),
        None,
    )
    reserved = any(
        x["vehicleId"] == vehicle["id"] and x["bookingStatus"] == "confirmed"
        for x in state["bookings"].values()
    )
    vehicle.pop("currentBookingId", None)
    vehicle.pop("currentAssignmentId", None)
    if active:
        vehicle.update(operationalStatus="rented", currentBookingId=active["id"])
    elif assigned:
        vehicle.update(operationalStatus="assigned", currentAssignmentId=assigned["id"])
    elif vehicle["operationalStatus"] not in HOLDS:
        vehicle["operationalStatus"] = "reserved" if reserved else "available"


def read_rows(state, resource, actor=None):
    defaults(state)
    if resource == "notifications":
        from .fleet_alerts import notifications_for

        result = deepcopy(list(notifications_for(state).values()))
    else:
        result = deepcopy(list(state[resource].values()))
    for row in result:
        if resource == "vehicles":
            refresh_vehicle(state, row)
            row["ownerName"] = state["owners"].get(row["ownerId"], {}).get("name", "")
        elif resource == "owners":
            row["email"] = row.get("email", "")
            row["vehicleCount"] = sum(v["ownerId"] == row["id"] for v in state["vehicles"].values())
        elif resource == "customers":
            bookings = [b for b in state["bookings"].values() if b["customerId"] == row["id"]]
            row.update(
                name=f"{row['firstName']} {row['lastName']}",
                fullName=f"{row['firstName']} {row['lastName']}",
                totalRentals=sum(b["bookingStatus"] == "completed" for b in bookings),
                totalBookings=len(bookings),
                totalSpend=money(sum(b.get("paidAmount", 0) for b in bookings)),
            )
        elif resource == "compliance":
            days = (date.fromisoformat(row["expiryDate"]) - date.fromisoformat(today())).days
            row["status"] = (
                "Expired"
                if days < 0
                else "Expiring Soon"
                if days <= settings(state)["complianceNoticeDays"]
                else "Valid"
            )
        elif resource == "notifications":
            row["read"] = bool(actor and actor["id"] in row.get("readBy", []))
            row.pop("readBy", None)
        elif resource == "documents":
            row["fileUrl"] = f"/api/admin/files/{row['fileId']}"
        elif resource == "bookings":
            for key in ("accessHash", "accessExpiresAt", "requestHash", "receipt"):
                row.pop(key, None)
    return sorted(result, key=lambda x: x.get("createdAt", ""), reverse=True)


def write_resource(state, actor, resource, payload, key=None):
    if resource not in MODELS:
        fail("This resource cannot be edited directly.", 405)
    if resource in {"settings", "categories", "locations"} and actor["role"] != "super_admin":
        fail("Only a super administrator can change company settings and catalogs.", 403)
    if key and resource in {
        "assignments",
        "maintenance",
        "compliance",
        "documents",
        "service_jobs",
    }:
        fail("Create a renewal or use the workflow action for this record.", 405)
    defaults(state)
    old = find(state, resource, key) if key else {}
    if key and payload.get("version") is not None and payload["version"] != old.get("version", 1):
        fail("This record was edited by another administrator. Refresh before saving.", 409)
    model = MODELS[resource]
    fields = model.model_fields
    # Read-only display fields are accepted for compatibility, but never trusted.
    unknown = set(payload) - set(fields) - DERIVED
    if unknown:
        fail("Unsupported fields: " + ", ".join(sorted(unknown)))
    data = validate(
        model,
        {
            **{k: v for k, v in old.items() if k in fields},
            **{k: v for k, v in payload.items() if k in fields},
        },
    )
    if "vehicleId" in data:
        vehicle = find(state, "vehicles", data["vehicleId"])
        data.update(
            vehicleReg=vehicle["registrationNumber"],
            vehicleName=f"{vehicle['brand']} {vehicle['model']}",
        )
    if resource == "vehicles":
        owner = find(state, "owners", data["ownerId"])
        if not any(c["slug"] == data["category"] for c in state["categories"].values()):
            fail("Choose a configured vehicle category.")
        data["ownerName"] = owner["name"]
        normalized = "".join(data["registrationNumber"].upper().split())
        for vehicle in state["vehicles"].values():
            if vehicle["id"] != key and (
                "".join(vehicle["registrationNumber"].upper().split()) == normalized
                or vehicle["slug"] == data["slug"]
            ):
                fail("Registration number and listing slug must be unique.", 409)
        if data["operationalStatus"] in {"rented", "assigned", "reserved"} and data[
            "operationalStatus"
        ] != old.get("operationalStatus"):
            fail("Use booking or assignment actions to set this status.")
        if old and data["mileage"] < old["mileage"]:
            fail("Mileage cannot decrease.")
        if old and data["operationalStatus"] != old["operationalStatus"]:
            if any(
                b["vehicleId"] == key and b["bookingStatus"] == "active"
                for b in state["bookings"].values()
            ) or any(
                a["vehicleId"] == key and a["status"] == "Active" and a["startDate"] <= today()
                for a in state["assignments"].values()
            ):
                fail("Return this vehicle before changing its operational status.", 409)
            if not data["statusChangeReason"]:
                fail("Provide a reason for the status change.")
            data["statusChangedAt"] = now()
    elif resource == "assignments":
        dates(data["startDate"], data["expectedReturnDate"])
        if unavailable(state, vehicle, data["startDate"], data["expectedReturnDate"]):
            fail("Vehicle is unavailable for this assignment.", 409)
        if data["mileageOut"] < vehicle["mileage"]:
            fail("Mileage out cannot be below the current odometer.")
        data["status"] = "Active"
        if data["startDate"] <= today():
            vehicle["mileage"] = data["mileageOut"]
            touch(vehicle)
    elif resource == "service_jobs":
        if data["expectedReturnDate"] < data["dateOut"]:
            fail("Expected return cannot precede the date sent.")
        data.update(status="Pending", reference="SVC-" + uuid4().hex[:12].upper())
        data["vehicleSnapshot"] = deepcopy(vehicle)
        previous = max(
            (r for r in state["maintenance"].values() if r["vehicleId"] == vehicle["id"]),
            key=lambda r: (r["date"], r["mileage"]),
            default=None,
        )
        data["lastService"] = deepcopy(previous)
    elif resource == "maintenance":
        if data["date"] > today():
            fail("Completed service cannot be in the future. Use a hold for planned work.")
        if (
            data.get("nextServiceMileage") is not None
            and data["nextServiceMileage"] <= data["mileage"]
        ):
            fail("Next service mileage must be greater than the service mileage.")
        if data.get("nextServiceDate") and data["nextServiceDate"] <= data["date"]:
            fail("Next service date must be after the completed service date.")
        data["totalCost"] = money(
            Decimal(str(data["partsCost"])) + Decimal(str(data["labourCost"]))
        )
        latest = max(
            (r for r in state["maintenance"].values() if r["vehicleId"] == data["vehicleId"]),
            key=lambda r: (r["date"], r["mileage"]),
            default=None,
        )
        vehicle["mileage"] = max(vehicle["mileage"], data["mileage"])
        if latest is None or (data["date"], data["mileage"]) >= (latest["date"], latest["mileage"]):
            vehicle["nextServiceMileage"] = (
                data["nextServiceMileage"]
                if data.get("nextServiceMileage") is not None
                else data["mileage"] + settings(state)["serviceIntervalKm"]
            )
            vehicle["nextServiceDate"] = data.get("nextServiceDate")
        touch(vehicle)
    elif resource == "compliance":
        if data["expiryDate"] < data["issueDate"]:
            fail("Expiry must be on or after the issue date.")
    elif resource == "documents":
        file = find(state, "files", data["fileId"])
        if file["kind"] != "document":
            fail("Choose an uploaded private document.")
        if (
            data.get("expiryDate")
            and data.get("issueDate")
            and data["expiryDate"] < data["issueDate"]
        ):
            fail("Expiry must be on or after issue date.")
        data.update(fileSize=f"{file['size'] / 1024:.1f} KB", uploadedAt=now())
        certification = data.pop("compliance", None)
        if certification is not None:
            compliance_type = {
                "Insurance Certificate": "Insurance",
                "Fitness Certificate": "Fitness Certificate",
                "MVL": "MVL",
                "Licence": "Licence",
            }.get(data["documentType"])
            if not compliance_type or not data.get("issueDate") or not data.get("expiryDate"):
                fail("Compliance documents require a supported type, issue date and expiry date.")
            if compliance_type == "Insurance" and (
                not certification.get("company") or not certification.get("policyNumber")
            ):
                fail("Insurance certificates require an insurance company and policy number.")
            record = write_resource(
                state,
                actor,
                "compliance",
                {
                    **certification,
                    "vehicleId": data["vehicleId"],
                    "complianceType": compliance_type,
                    "issueDate": data["issueDate"],
                    "expiryDate": data["expiryDate"],
                    "documentUrl": f"/api/admin/files/{data['fileId']}",
                    "notes": data.get("notes", ""),
                },
            )
            data["complianceId"] = record["id"]
    row = new_record(state, resource, data, key) if not key else old
    if key:
        row.update(data)
        touch(row)
    audit(
        state,
        actor,
        "Updated" if key else "Created",
        resource,
        row,
        data.get("statusChangeReason", ""),
    )
    if resource == "assignments":
        refresh_vehicle(state, vehicle)
        touch(vehicle)
    return row


def quote(state, vehicle, start, end, pickup_id, return_id):
    days = dates(start, end)
    pickup = deepcopy(find(state, "locations", pickup_id))
    dropoff = deepcopy(find(state, "locations", return_id))
    policy = settings(state)
    for location in (pickup, dropoff):
        if location["id"] == "airport-mru":
            location["pickupFee"] = location["dropoffFee"] = policy["airportDeliveryFee"]
        if location["id"] == "hotel-delivery-islandwide":
            location["pickupFee"] = location["dropoffFee"] = policy["hotelDeliveryFee"]
    base = Decimal(str(vehicle["dailyRate"])) * days
    fees = Decimal(str(pickup["pickupFee"])) + Decimal(str(dropoff["dropoffFee"]))
    total = base + fees
    vat = Decimal(str(policy["vatRate"]))
    return (
        {
            "days": days,
            "dailyRate": vehicle["dailyRate"],
            "baseAmount": money(base),
            "locationFee": money(fees),
            "vatIncluded": money(total * vat / (100 + vat)),
            "estimatedTotal": money(total),
            "currency": "MUR",
        },
        pickup,
        dropoff,
    )


def create_booking(state, payload, actor, token_secret):
    defaults(state)
    data = validate(BookingInput, payload)
    request_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    idem_id = hashlib.sha256(data["idempotencyKey"].encode()).hexdigest()
    existing = state["idempotency"].get(idem_id)
    if existing:
        if existing["requestHash"] != request_hash:
            fail("This request key was already used for different booking details.", 409)
        row = find(state, "bookings", existing["bookingId"])
    else:
        vehicle = find(state, "vehicles", data["vehicleId"])
        if not actor and not vehicle["published"]:
            fail("Vehicle listing not found.", 404)
        pricing, pickup, dropoff = quote(
            state,
            vehicle,
            data["pickupDate"],
            data["returnDate"],
            data["pickupLocationId"],
            data["returnLocationId"],
        )
        if unavailable(state, vehicle, data["pickupDate"], data["returnDate"]):
            fail(
                "Vehicle is unavailable for these dates. Please choose another vehicle or date.",
                409,
            )
        # Never merge unauthenticated submissions into another customer's verified profile by email.
        customer_data = {k: v for k, v in data["customer"].items() if k != "specialRequest"}
        customer = new_record(
            state,
            "customers",
            {
                **customer_data,
                "address": "",
                "licenceNumber": "",
                "licenceExpiryDate": "",
                "licenceCountry": "",
                "idOrPassport": "",
                "notes": "",
            },
        )
        snapshot = {
            k: vehicle[k]
            for k in (
                "id",
                "slug",
                "brand",
                "model",
                "year",
                "category",
                "dailyRate",
                "transmission",
                "fuelType",
                "seats",
            )
        }
        snapshot["photo"] = next(iter(vehicle["photos"]), "")
        row = new_record(
            state,
            "bookings",
            {
                "reference": "OCR-" + uuid4().hex[:16].upper(),
                "customerId": customer["id"],
                "customerName": f"{customer['firstName']} {customer['lastName']}",
                "customerEmail": customer["email"],
                "customerPhone": customer["phone"],
                "vehicleId": vehicle["id"],
                "vehicleName": f"{vehicle['brand']} {vehicle['model']}",
                "vehicleReg": vehicle["registrationNumber"],
                "pickupDate": data["pickupDate"],
                "returnDate": data["returnDate"],
                "pickupLocation": pickup["name"],
                "returnLocation": dropoff["name"],
                "dailyRate": pricing["dailyRate"],
                "days": pricing["days"],
                "estimatedAmount": pricing["estimatedTotal"],
                "finalAmount": pricing["estimatedTotal"],
                "securityDeposit": settings(state)["defaultDeposit"],
                "bookingStatus": "pending",
                "paymentStatus": "Unpaid",
                "paidAmount": 0,
                "specialRequests": data["customer"].get("specialRequest", ""),
                "receipt": {
                    "vehicle": snapshot,
                    "pickupLocation": pickup,
                    "returnLocation": dropoff,
                    "pricing": pricing,
                    "customer": data["customer"],
                },
                "accessExpiresAt": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
            },
        )
        state["idempotency"][idem_id] = {
            "id": idem_id,
            "requestHash": request_hash,
            "bookingId": row["id"],
        }
        audit(state, actor, "Booking requested", "bookings", row)
        notification(state, "booking_request", "New booking request", row["reference"])
    token = hmac.new(
        token_secret.encode(), ("receipt:" + row["id"]).encode(), hashlib.sha256
    ).hexdigest()
    row["accessHash"] = hashlib.sha256(token.encode()).hexdigest()
    return {**receipt(row), "accessToken": token}


def receipt(row):
    return {
        **deepcopy(row["receipt"]),
        "id": row["id"],
        "reference": row["reference"],
        "vehicleId": row["vehicleId"],
        "pickupDate": row["pickupDate"],
        "returnDate": row["returnDate"],
        "status": row["bookingStatus"],
        "createdAt": row["createdAt"],
    }


def validate_inspection_photos(state, photos):
    for url in photos:
        if not url.startswith("/api/admin/files/"):
            fail("Upload private inspection images before saving the handover.")
        row = find(state, "files", url.removeprefix("/api/admin/files/"))
        if row["kind"] != "document" or not row["contentType"].startswith("image/"):
            fail("Inspection photos must be uploaded image files.")


def booking_action(state, actor, key, action, payload):
    row = find(state, "bookings", key)
    vehicle = find(state, "vehicles", row["vehicleId"])
    status = row["bookingStatus"]
    transitions = {
        "confirm": ({"pending"}, "confirmed"),
        "reject": ({"pending"}, "rejected"),
        "cancel": ({"pending", "confirmed"}, "cancelled"),
        "checkout": ({"confirmed"}, "active"),
        "checkin": ({"active"}, "completed"),
    }
    if action == "payment":
        from pydantic import BaseModel, Field

        class Payment(BaseModel):
            paidAmount: float = Field(ge=0, le=1_000_000_000, allow_inf_nan=False)
            reason: str = Field(min_length=1, max_length=1000)

        data = validate(Payment, payload)
        row["paidAmount"] = money(data["paidAmount"])
        row["paymentStatus"] = (
            "Fully Paid"
            if row["paidAmount"] >= row["finalAmount"]
            else "Deposit Paid"
            if row["paidAmount"] > 0
            else "Unpaid"
        )
    else:
        if action not in transitions:
            fail("Unknown booking action.", 404)
        allowed, target = transitions[action]
        if status not in allowed:
            fail(f"Cannot {action} a {status} booking.", 409)
        if action in {"confirm", "checkout"}:
            if action == "confirm":
                dates(row["pickupDate"], row["returnDate"])
            if unavailable(state, vehicle, row["pickupDate"], row["returnDate"], row["id"]):
                fail("Vehicle is no longer available for this booking.", 409)
        if action in {"reject", "cancel"}:
            if (
                not isinstance(payload.get("reason"), str)
                or not 1 <= len(payload["reason"].strip()) <= 1000
            ):
                fail("Provide a reason (up to 1000 characters).")
            row["statusReason"] = payload["reason"].strip()
        elif action == "checkout":
            data = validate(CheckoutInput, payload)
            validate_inspection_photos(state, data["checkoutPhotos"])
            if not row["pickupDate"] <= today() <= row["returnDate"]:
                fail("Check-out must occur within the confirmed rental dates.")
            if data["mileageOut"] < vehicle["mileage"]:
                fail("Mileage out cannot decrease the odometer.")
            if any(
                b["id"] != key
                and b["vehicleId"] == vehicle["id"]
                and b["bookingStatus"] == "active"
                for b in state["bookings"].values()
            ):
                fail("The previous rental must be returned first.", 409)
            if any(
                a["vehicleId"] == vehicle["id"]
                and a["status"] == "Active"
                and a["startDate"] <= today()
                for a in state["assignments"].values()
            ):
                fail("The vehicle assignment must be returned first.", 409)
            row.update(data, checkedOutAt=now())
            vehicle["mileage"] = data["mileageOut"]
        elif action == "checkin":
            data = validate(CheckinInput, payload)
            validate_inspection_photos(state, data["checkinPhotos"])
            if data["mileageIn"] < max(row.get("mileageOut", 0), vehicle["mileage"]):
                fail("Mileage in cannot be below mileage out or the current odometer.")
            row.update(
                {k: v for k, v in data.items() if k not in {"finalVehicleStatus", "serviceReason"}},
                checkedInAt=now(),
            )
            vehicle.update(
                mileage=data["mileageIn"],
                operationalStatus=data["finalVehicleStatus"],
                statusChangeReason=data["serviceReason"],
            )
            notification(state, "vehicle_returned", "Vehicle returned", row["reference"])
        row["bookingStatus"] = target
    touch(row)
    refresh_vehicle(state, vehicle)
    touch(vehicle)
    audit(state, actor, action.title(), "bookings", row, payload.get("reason", ""))
    return row


def assignment_action(state, actor, key, action, payload):
    row = find(state, "assignments", key)
    if row["status"] != "Active":
        fail("This assignment has already ended.", 409)
    vehicle = find(state, "vehicles", row["vehicleId"])
    if action == "end":
        mileage = payload.get("mileageIn")
        if (
            not isinstance(mileage, int)
            or isinstance(mileage, bool)
            or not max(row["mileageOut"], vehicle["mileage"]) <= mileage <= 10_000_000
        ):
            fail("Enter valid return mileage at or above the current odometer.")
        if row["startDate"] > today():
            fail("Cancel a future assignment instead of returning it.")
        row.update(status="Completed", actualReturnDate=today(), mileageIn=mileage)
        vehicle["mileage"] = mileage
    elif action == "cancel":
        if row["startDate"] <= today():
            fail("Return an assignment that has already started.")
        if not payload.get("reason"):
            fail("A cancellation reason is required.")
        row.update(status="Cancelled", cancellationReason=str(payload["reason"])[:1000])
    else:
        fail("Unknown assignment action.", 404)
    row["notes"] = str(payload.get("notes", row.get("notes", "")))[:4000]
    touch(row)
    refresh_vehicle(state, vehicle)
    touch(vehicle)
    audit(state, actor, action.title(), "assignments", row)
    return row
