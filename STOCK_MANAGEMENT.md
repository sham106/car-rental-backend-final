# Stock management

## Enable

1. Ensure migrations 002 and 006 have already been applied to the same Supabase project used by the backend.
2. Run the entire `migrations/007_stock_management.sql` in Supabase SQL Editor. It adds stock tables and extends the existing atomic snapshot/commit functions without deleting fleet records. Do not reapply older function migrations afterward.
3. Deploy/restart the backend and deploy the frontend. Open `/admin/stock` (Stock Management in the sidebar).

## Use

Create an item with its unit, supplier, location, unit cost, description, and low-stock threshold. Leave SKU blank to generate a unique sequential code such as STK-000001, or enter a custom unique SKU. Leaving SKU blank while editing keeps the current code. New items start at zero. Record opening inventory with **Stock in**, reason **Opening stock**. Use **Stock out** for withdrawals; record the recipient or reason and optionally a vehicle, job, or invoice reference. Quantities support three decimal places.

Remaining stock is total received minus total issued. Zero stock is flagged separately; a positive balance at or below the threshold is low stock. The stock value is an estimate at each item's current unit cost, not an accounting valuation. Totals exclude archived items.

Archive items by unchecking Active in Edit details; history remains available under Archived items. Reactivate before making further movements. Units cannot change after movements exist. Correct a movement through an opposite movement with an explanatory reason; history cannot be edited or deleted through the API.

## Integrity and access

All stock routes require an authenticated administrator. Public and authenticated Supabase clients have no direct table permissions. The server uses the existing revision-checked transaction layer to revalidate withdrawals against concurrent changes. Repeated movement requests with the same request ID are deduplicated. Item edits require the latest version. Stock data is not added to the PWA offline cache.

## Validation

`python -m pytest tests/test_stock.py tests/test_fleet.py -q`

Live Supabase migration execution is a separate deployment step; local tests use the production domain and transaction code with an in-memory persistence boundary.
