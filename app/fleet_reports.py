"""Operational reports. Revenue means completed rental charges, not cash receipts."""

import csv
import io
from datetime import date

from .fleet_domain import money, read_rows, today


def reports(state):
    vehicles = read_rows(state, "vehicles")
    counts = {
        s: sum(v["operationalStatus"] == s for v in vehicles)
        for s in (
            "available",
            "reserved",
            "rented",
            "assigned",
            "in_service",
            "compliance_hold",
            "inactive",
        )
    }
    total = len(vehicles)
    utilization = {
        "totalVehicles": total,
        "activeRentals": counts["rented"],
        "assigned": counts["assigned"],
        "available": counts["available"],
        "reserved": counts["reserved"],
        "inService": counts["in_service"],
        "complianceHold": counts["compliance_hold"],
        "inactive": counts["inactive"],
        "utilizationRate": round(100 * (counts["rented"] + counts["assigned"]) / total)
        if total
        else 0,
    }
    categories = {}
    performance = []
    for vehicle in vehicles:
        bookings = [
            b
            for b in state["bookings"].values()
            if b["vehicleId"] == vehicle["id"] and b["bookingStatus"] == "completed"
        ]
        revenue = money(sum(b["finalAmount"] for b in bookings))
        maintenance = money(
            sum(
                m["totalCost"]
                for m in state["maintenance"].values()
                if m["vehicleId"] == vehicle["id"]
            )
        )
        insurance = money(
            sum(
                c.get("premium", 0)
                for c in state["compliance"].values()
                if c["vehicleId"] == vehicle["id"] and c["complianceType"] == "Insurance"
            )
        )
        days = sum(b["days"] for b in bookings)
        operating_days = max(
            1,
            (
                date.fromisoformat(today())
                - date.fromisoformat(vehicle.get("createdAt", today())[:10])
            ).days
            + 1,
        )
        performance.append(
            {
                "vehicleId": vehicle["id"],
                "registrationNumber": vehicle["registrationNumber"],
                "name": f"{vehicle['brand']} {vehicle['model']}",
                "purchaseValue": vehicle.get("purchaseValue", 0),
                "rentalRevenue": revenue,
                "maintenanceCost": maintenance,
                "insuranceCost": insurance,
                "daysRented": days,
                "idleDays": max(0, operating_days - days),
                "utilizationPercent": min(100, round(100 * days / operating_days)),
                "netMargin": money(revenue - maintenance - insurance),
            }
        )
        category = categories.setdefault(vehicle["category"], {"revenue": 0, "bookingsCount": 0})
        category["revenue"] = money(category["revenue"] + revenue)
        category["bookingsCount"] += len(bookings)
    return {
        "utilization": utilization,
        "performance": performance,
        "revenueByCategory": [{"category": k.upper(), **v} for k, v in categories.items()],
        "definitions": {
            "revenue": "Completed rental charges; not a tax or cash accounting statement.",
            "period": "All recorded history since fleet onboarding.",
        },
    }


def export_csv(state, kind):
    specs = {
        "owners": ("owners", ["name", "ownerType", "vehicleCount", "revenueSplitPercentage"]),
        "utilization": (
            "vehicles",
            [
                "registrationNumber",
                "brand",
                "model",
                "category",
                "operationalStatus",
                "mileage",
                "ownerName",
            ],
        ),
        "revenue": (
            "bookings",
            [
                "reference",
                "customerName",
                "vehicleName",
                "pickupDate",
                "returnDate",
                "finalAmount",
                "paidAmount",
                "bookingStatus",
            ],
        ),
        "maintenance": (
            "maintenance",
            [
                "vehicleReg",
                "date",
                "mileage",
                "serviceType",
                "garage",
                "partsCost",
                "labourCost",
                "totalCost",
            ],
        ),
        "compliance": (
            "compliance",
            ["vehicleReg", "complianceType", "company", "policyNumber", "expiryDate", "status"],
        ),
    }
    if kind not in specs:
        return None
    resource, fields = specs[kind]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in read_rows(state, resource):
        values = []
        for field in fields:
            value = str(row.get(field, ""))
            # Prevent spreadsheet formula execution while preserving ordinary CSV escaping.
            if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(
                ("\t", "\r", "\n")
            ):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return "\ufeff" + output.getvalue()
