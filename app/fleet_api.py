import base64
import hashlib
import hmac
import io
import re
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from .auth import Admin
from .fleet_domain import (
    PUBLIC_VEHICLE_FIELDS,
    allocations,
    assignment_action,
    audit,
    booking_action,
    create_booking,
    dates,
    defaults,
    fail,
    find,
    new_record,
    now,
    quote,
    read_rows,
    receipt,
    settings,
    today,
    unavailable,
    write_resource,
)
from .fleet_models import MODELS
from .fleet_reports import export_csv, reports

router = APIRouter(prefix="/api", tags=["Fleet operations"])
READABLE = set(MODELS) | {"bookings", "audit", "notifications"}


@router.post("/admin/import/parse")
async def parse_spreadsheet(request: Request, admin: Admin, payload: dict = Body(...)):
    from .fleet_import import parse_import

    return parse_import(payload, await state_for(request))


def actor(admin):
    return admin.model_dump(mode="json")


async def state_for(request):
    _, state = await request.app.state.fleet.snapshot()
    defaults(state)
    return state


async def transaction(request, operation):
    def run(state):
        defaults(state)
        result = operation(state)
        return result, allocations(state)

    return await request.app.state.fleet.transact(run)


def public_limit(request):
    address = request.client.host if request.client else "unknown"
    request.app.state.limiter.check("public:" + address, 20)


@router.get("/admin/data")
async def overview(request: Request, admin: Admin):
    state = await state_for(request)
    return {r: read_rows(state, r, actor(admin)) for r in READABLE}


@router.get("/admin/records/{resource}")
async def list_records(
    resource: str,
    request: Request,
    admin: Admin,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
):
    if resource not in READABLE:
        fail("Resource not found.", 404)
    rows = read_rows(await state_for(request), resource, actor(admin))
    return {"items": rows[offset : offset + limit], "total": len(rows)}


@router.post("/admin/records/{resource}")
async def create_record(resource: str, request: Request, admin: Admin, payload: dict = Body(...)):
    if resource in {"settings"}:
        fail("Update company settings using the existing company record.", 405)
    row = await transaction(request, lambda s: write_resource(s, actor(admin), resource, payload))
    return row


@router.patch("/admin/records/{resource}/{key}")
async def update_record(
    resource: str, key: str, request: Request, admin: Admin, payload: dict = Body(...)
):
    return await transaction(
        request, lambda s: write_resource(s, actor(admin), resource, payload, key)
    )


@router.post("/admin/service-jobs/{key}/complete")
async def complete_service_job(key: str, request: Request, admin: Admin, payload: dict = Body(...)):
    from .service_jobs import complete_job

    return await transaction(request, lambda s: complete_job(s, actor(admin), key, payload))


@router.delete("/admin/records/documents/{key}")
async def delete_document(key: str, request: Request, admin: Admin):
    def delete(state):
        row = find(state, "documents", key)
        audit(state, actor(admin), "Deleted", "documents", row)
        del state["documents"][key]
        # File retained for audit/recovery; it is private and cannot be listed publicly.
        return {"deleted": True}

    return await transaction(request, delete)


@router.post("/admin/vehicles/bulk-publish")
async def bulk_publish(request: Request, admin: Admin, payload: dict = Body(...)):
    ids = payload.get("ids")
    published = payload.get("published")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 500
        or not all(isinstance(i, str) for i in ids)
        or not isinstance(published, bool)
    ):
        fail("Choose 1–500 vehicles and a publication state.")

    def update(state):
        for key in set(ids):
            write_resource(state, actor(admin), "vehicles", {"published": published}, key)
        return {"count": len(set(ids))}

    return await transaction(request, update)


@router.post("/admin/bookings")
async def admin_booking(request: Request, admin: Admin, payload: dict = Body(...)):
    return await transaction(
        request,
        lambda s: create_booking(
            s,
            payload,
            actor(admin),
            request.app.state.settings.supabase_secret_key.get_secret_value(),
        ),
    )


@router.post("/admin/bookings/{key}/{action}")
async def transition_booking(
    key: str, action: str, request: Request, admin: Admin, payload: dict = Body(...)
):
    await transaction(request, lambda s: booking_action(s, actor(admin), key, action, payload))
    return next(r for r in read_rows(await state_for(request), "bookings") if r["id"] == key)


@router.post("/admin/assignments/{key}/{action}")
async def transition_assignment(
    key: str, action: str, request: Request, admin: Admin, payload: dict = Body(...)
):
    return await transaction(
        request, lambda s: assignment_action(s, actor(admin), key, action, payload)
    )


@router.post("/admin/notifications/read")
async def notification_read(request: Request, admin: Admin, payload: dict = Body(...)):
    def update(state):
        from .fleet_alerts import notifications_for

        available = notifications_for(state)
        state["notifications"].update(available)
        rows = (
            list(state["notifications"].values())
            if payload.get("all") is True
            else [find(state, "notifications", payload.get("id", ""))]
        )
        for row in rows:
            row["readBy"] = list(set(row.get("readBy", [])) | {str(admin.id)})
        return {"updated": len(rows)}

    return await transaction(request, update)


@router.get("/admin/reports")
async def report(request: Request, admin: Admin):
    return reports(await state_for(request))


@router.get("/admin/reports/{kind}.csv")
async def report_csv(kind: str, request: Request, admin: Admin):
    result = export_csv(await state_for(request), kind)
    if result is None:
        fail("Report not found.", 404)
    return Response(
        result,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{kind}-{today()}.csv"'},
    )


@router.get("/public/vehicles")
async def public_vehicles(
    request: Request,
    search: str = "",
    category: str = "",
    brand: str = "",
    transmission: str = "",
    fuelType: str = "",
    minSeats: int = Query(0, ge=0),
    minPrice: float = Query(0, ge=0),
    maxPrice: float = Query(1e9, ge=0),
    sortBy: str = "recommended",
    pickupDate: str = "",
    returnDate: str = "",
    featured: bool = False,
):
    state = await state_for(request)
    if pickupDate or returnDate:
        dates(pickupDate, returnDate)
    result = []
    for vehicle in read_rows(state, "vehicles"):
        if not vehicle["published"] or (featured and not vehicle["featured"]):
            continue
        if (
            search.casefold()
            not in f"{vehicle['brand']} {vehicle['model']} {vehicle['category']}".casefold()
        ):
            continue
        if any(
            value and value != "all" and vehicle[field].casefold() != value.casefold()
            for field, value in [
                ("category", category),
                ("brand", brand),
                ("transmission", transmission),
                ("fuelType", fuelType),
            ]
        ):
            continue
        if vehicle["seats"] < minSeats or not minPrice <= vehicle["dailyRate"] <= maxPrice:
            continue
        if pickupDate and unavailable(state, vehicle, pickupDate, returnDate):
            continue
        result.append({k: v for k, v in vehicle.items() if k in PUBLIC_VEHICLE_FIELDS})
    sorters = {
        "price_asc": lambda v: v["dailyRate"],
        "price_desc": lambda v: -v["dailyRate"],
        "year_desc": lambda v: -v["year"],
        "recommended": lambda v: (not v["featured"], v["brand"], v["model"]),
    }
    return sorted(result, key=sorters.get(sortBy, sorters["recommended"]))


@router.get("/public/vehicles/{key}")
async def public_vehicle(key: str, request: Request):
    state = await state_for(request)
    vehicle = next(
        (
            v
            for v in read_rows(state, "vehicles")
            if v["published"] and (v["id"] == key or v["slug"] == key)
        ),
        None,
    )
    if not vehicle:
        fail("Vehicle listing not found.", 404)
    return {k: v for k, v in vehicle.items() if k in PUBLIC_VEHICLE_FIELDS}


@router.get("/public/vehicles/{key}/availability")
async def availability(key: str, request: Request, pickupDate: str, returnDate: str):
    dates(pickupDate, returnDate)
    state = await state_for(request)
    vehicle = find(state, "vehicles", key)
    if not vehicle["published"]:
        fail("Vehicle listing not found.", 404)
    reason = unavailable(state, vehicle, pickupDate, returnDate)
    return {"isAvailable": reason is None, **({"reason": reason} if reason else {})}


@router.get("/admin/availability")
async def admin_availability(
    request: Request,
    admin: Admin,
    vehicleId: str,
    startDate: str,
    endDate: str,
    excludeBookingId: str = "",
):
    dates(startDate, endDate)
    state = await state_for(request)
    return {
        "isAvailable": not unavailable(
            state, find(state, "vehicles", vehicleId), startDate, endDate, excludeBookingId
        )
    }


@router.get("/public/vehicles/{key}/ranges")
async def ranges(key: str, request: Request):
    state = await state_for(request)
    if not find(state, "vehicles", key)["published"]:
        fail("Vehicle listing not found.", 404)
    return [
        {
            "startDate": b["startDate"],
            "endDate": b["endDate"],
            "type": "confirmed_booking" if b["kind"] == "booking" else "maintenance",
            "label": "Unavailable",
        }
        for b in allocations(state)
        if b["vehicleId"] == key
    ]


@router.get("/public/categories")
async def categories(request: Request):
    state = await state_for(request)
    result = []
    for category in state["categories"].values():
        vehicles = [
            v
            for v in state["vehicles"].values()
            if v["published"] and v["category"] == category["slug"]
        ]
        result.append(
            {
                **category,
                "vehicleCount": len(vehicles),
                "startingDailyRate": min((v["dailyRate"] for v in vehicles), default=0),
            }
        )
    return result


@router.get("/public/locations")
async def locations(request: Request):
    state = await state_for(request)
    result = list(state["locations"].values())
    policy = settings(state)
    for row in result:
        if row["id"] in {"airport-mru", "hotel-delivery-islandwide"}:
            row["pickupFee"] = row["dropoffFee"] = policy[
                "airportDeliveryFee" if row["id"] == "airport-mru" else "hotelDeliveryFee"
            ]
    return result


@router.get("/public/quote")
async def public_quote(
    request: Request,
    vehicleId: str,
    pickupDate: str,
    returnDate: str,
    pickupLocationId: str,
    returnLocationId: str,
):
    state = await state_for(request)
    vehicle = find(state, "vehicles", vehicleId)
    if not vehicle["published"]:
        fail("Vehicle listing not found.", 404)
    pricing, _, _ = quote(
        state, vehicle, pickupDate, returnDate, pickupLocationId, returnLocationId
    )
    return pricing


@router.post("/public/bookings")
async def guest_booking(request: Request, payload: dict = Body(...)):
    public_limit(request)
    return await transaction(
        request,
        lambda s: create_booking(
            s, payload, None, request.app.state.settings.supabase_secret_key.get_secret_value()
        ),
    )


@router.post("/public/bookings/lookup")
async def lookup(request: Request, payload: dict = Body(...)):
    public_limit(request)
    state = await state_for(request)
    token = str(payload.get("accessToken", ""))
    row = next(
        (r for r in state["bookings"].values() if r["reference"] == payload.get("reference")), None
    )
    if (
        not row
        or not hmac.compare_digest(
            row.get("accessHash", ""), hashlib.sha256(token.encode()).hexdigest()
        )
        or row.get("accessExpiresAt", "") < now()
    ):
        fail("Booking not found or access has expired.", 404)
    return receipt(row)


@router.post("/admin/files")
async def upload(request: Request, admin: Admin, payload: dict = Body(...)):
    kind = payload.get("kind")
    if kind not in {"photo", "document"}:
        fail("Choose photo or document upload.")
    try:
        raw = base64.b64decode(payload.get("content", ""), validate=True)
    except (ValueError, TypeError):
        fail("Invalid file encoding.")
    if not 1 <= len(raw) <= 10 * 1024 * 1024:
        fail("Upload a file up to 10 MB.")
    mime = payload.get("contentType")
    if mime in {"image/jpeg", "image/png", "image/webp"}:
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(raw)) as img:
                if img.width * img.height > 25_000_000:
                    fail("Image exceeds 25 megapixels.")
                img.load()
                output = io.BytesIO()
                img.convert("RGB").save(output, format="JPEG", quality=90)
                raw = output.getvalue()
                mime = "image/jpeg"
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            fail("The image file is invalid.")
    elif mime == "application/pdf" and kind == "document" and raw.startswith(b"%PDF-"):
        pass
    else:
        fail("Supported files: JPEG, PNG, WebP and PDF documents.")
    store = request.app.state.fleet
    file_id = str(uuid4())
    path = f"{kind}/{file_id}.{'pdf' if mime == 'application/pdf' else 'jpg'}"
    try:
        response = await store.client.post(
            store.settings.supabase_url
            + "/storage/v1/object/"
            + store.settings.storage_bucket
            + "/"
            + path,
            headers={**store.headers(), "Content-Type": mime, "x-upsert": "false"},
            content=raw,
        )
    except httpx.RequestError:
        fail("File storage unavailable. Please retry.", 503)
    if response.status_code >= 400:
        fail("File upload failed. Check the private fleet-files bucket and backend key.", 503)

    def record(state):
        row = new_record(
            state,
            "files",
            {
                "path": path,
                "kind": kind,
                "contentType": mime,
                "size": len(raw),
                "name": str(payload.get("name", "file"))[:160],
                "uploadedBy": str(admin.id),
            },
            file_id,
        )
        audit(state, actor(admin), "File uploaded", "documents", row)
        return {
            "id": file_id,
            "url": f"/api/public/photos/{file_id}"
            if kind == "photo"
            else f"/api/admin/files/{file_id}",
            "size": len(raw),
        }

    try:
        return await transaction(request, record)
    except HTTPException:
        # Best-effort compensation; an orphan remains private if storage is unavailable.
        try:
            await store.client.request(
                "DELETE",
                store.settings.supabase_url + "/storage/v1/object/" + store.settings.storage_bucket,
                headers=store.headers(),
                json={"prefixes": [path]},
            )
        except httpx.RequestError:
            pass
        raise


async def serve_file(request, key, public=False):
    state = await state_for(request)
    row = find(state, "files", key)
    if public and (
        row["kind"] != "photo"
        or not any(
            v["published"] and f"/api/public/photos/{key}" in v["photos"]
            for v in state["vehicles"].values()
        )
    ):
        fail("Photo not found.", 404)
    store = request.app.state.fleet
    try:
        response = await store.client.get(
            store.settings.supabase_url
            + "/storage/v1/object/"
            + store.settings.storage_bucket
            + "/"
            + row["path"],
            headers=store.headers(),
        )
    except httpx.RequestError:
        fail("File storage unavailable.", 503)
    if response.status_code >= 400:
        fail("File unavailable.", 404)
    extension = "pdf" if row["contentType"] == "application/pdf" else "jpg"
    return Response(
        response.content,
        media_type=row["contentType"],
        headers={
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Content-Disposition": f'inline; filename="{key}.{extension}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/admin/files/{key}")
async def admin_file(key: str, request: Request, admin: Admin):
    return await serve_file(request, key)


@router.get("/public/photos/{key}")
async def public_photo(key: str, request: Request):
    return await serve_file(request, key, public=True)


@router.post("/admin/import/vehicles")
async def import_vehicles(request: Request, admin: Admin, payload: dict = Body(...)):
    rows = payload.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        fail("Import between 1 and 500 rows.")

    def commit(state):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                fail(f"Invalid import row {index + 1}.")
            owner = next(
                (
                    o
                    for o in state["owners"].values()
                    if o["name"].casefold() == str(row.get("ownerName", "")).casefold()
                ),
                None,
            )
            if not owner:
                fail(f"Row {index + 1}: create the owner before importing their vehicles.")
            data = {k: v for k, v in row.items() if k in MODELS["vehicles"].model_fields}
            slug = re.sub(
                "[^a-z0-9]+",
                "-",
                "-".join(
                    str(row.get(k, "")) for k in ("brand", "model", "registrationNumber")
                ).lower(),
            ).strip("-")
            data.update(
                ownerId=owner["id"], slug=slug, published=False, operationalStatus="available"
            )
            try:
                write_resource(state, actor(admin), "vehicles", data)
            except HTTPException as error:
                fail(f"Row {index + 1}: {error.detail}", error.status_code)
        return {"importedCount": len(rows)}

    return await transaction(request, commit)
