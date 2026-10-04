"""Inventory ledger: quantities change only through audited, atomic movements."""
import re
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import Field
from .fleet_models import Input, Name, Text, Money
from .fleet_domain import audit, fail, find, new_record, touch, validate

Quantity = Annotated[Decimal, Field(ge=0, le=1_000_000_000, max_digits=13, decimal_places=3, allow_inf_nan=False)]

class ItemInput(Input):
    name: Name
    sku: Annotated[str, Field(max_length=160)] = ""
    category: Text = ""
    unit: Name = "pieces"
    supplier: Text = ""
    location: Text = ""
    description: Text = ""
    unitCost: Money = 0
    lowStockThreshold: Quantity = Decimal("5")
    active: bool = True
    version: int | None = None
    requestId: str = Field(min_length=16, max_length=128)

class MovementInput(Input):
    direction: Literal["in", "out"]
    quantity: Annotated[Decimal, Field(gt=0, le=1_000_000_000, max_digits=13, decimal_places=3, allow_inf_nan=False)]
    reason: Name
    reference: Text = ""
    requestId: str = Field(min_length=16, max_length=128)


def item_view(state, item):
    entries = [m for m in state["stock_movements"].values() if m["itemId"] == item["id"]]
    received = sum((Decimal(str(m["quantity"])) for m in entries if m["direction"] == "in"), Decimal(0))
    issued = sum((Decimal(str(m["quantity"])) for m in entries if m["direction"] == "out"), Decimal(0))
    balance = received - issued
    return {**item, "received": float(received), "issued": float(issued), "remaining": float(balance),
            "status": "Out of stock" if balance == 0 else "Low stock" if balance <= Decimal(str(item["lowStockThreshold"])) else "In stock"}


def save_item(state, actor, payload, key=None):
    data = validate(ItemInput, payload)
    request_id = data.pop("requestId")
    version = data.pop("version", None)
    data["lowStockThreshold"] = float(data["lowStockThreshold"])
    if not key:
        for existing in state["stock_items"].values():
            if existing.get("requestId") == request_id:
                if existing.get("creatorId") != actor["id"] or any((existing.get("requestedSku", existing["sku"]) if k == "sku" else existing.get(k)) != v for k, v in data.items()):
                    fail("This request was already used for a different item.", 409)
                return item_view(state, existing)
    requested_sku = data["sku"]
    old = find(state, "stock_items", key) if key else None
    if not data["sku"]:
        if old:
            data["sku"] = old["sku"]
        else:
            numbers = [int(match.group(1)) for item in state["stock_items"].values()
                       if (match := re.fullmatch(r"STK-([0-9]{1,12})", item["sku"], re.IGNORECASE))]
            data["sku"] = f"STK-{max(numbers, default=0) + 1:06d}"

    if old and version != old["version"]:
        fail("This item changed. Refresh before editing it.", 409)
    if any(i["id"] != key and i["sku"].casefold() == data["sku"].casefold() for i in state["stock_items"].values()):
        fail("An item with this SKU already exists.", 409)
    if old and old["unit"] != data["unit"] and any(m["itemId"] == key for m in state["stock_movements"].values()):
        fail("The unit cannot change after stock movements have been recorded.")
    if old:
        old.update(data)
        touch(old)
        row = old
    else:
        row = new_record(state, "stock_items", {**data, "requestId": request_id, "creatorId": actor["id"], "requestedSku": requested_sku})
    audit(state, actor, "Updated" if old else "Created", "stock_items", row)
    return item_view(state, row)


def move_stock(state, actor, key, payload):
    data = validate(MovementInput, payload)
    data["quantity"] = float(data["quantity"])
    for existing in state["stock_movements"].values():
        if existing["requestId"] == data["requestId"]:
            if existing["itemId"] != key or existing["actorId"] != actor["id"] or any(existing[k] != v for k, v in data.items()):
                fail("This request was already used for a different movement.", 409)
            return existing
    item = find(state, "stock_items", key)
    if not item["active"]:
        fail("Reactivate this item before recording stock movements.")
    balance = item_view(state, item)["remaining"]
    if data["direction"] == "out" and data["quantity"] > balance:
        fail("Not enough stock available for this withdrawal.", 409)
    if data["direction"] == "in" and balance + data["quantity"] > 1_000_000_000:
        fail("Maximum stock quantity exceeded.")
    row = new_record(state, "stock_movements", {**data, "itemId": key, "actorId": actor["id"], "actorName": actor["name"],
        "balanceAfter": round(balance + data["quantity"] * (1 if data["direction"] == "in" else -1), 3)})
    touch(item)
    audit(state, actor, "Stock received" if data["direction"] == "in" else "Stock issued", "stock_items", item, data["reason"])
    return row
