"""F1 — Balance -> Zoho Products (+ dwh): items, prices, stock. Every 15 min.

1. Read Items, Prices, Stocks changes one by one (LastReadChange cursor per service).
2. Upsert into dwh.* and move the cursor in the same transaction.
3. Build Zoho Products for every changed item and upsert them by INT_Balance_Code,
   100 per call. A failed record is logged and does not block the rest.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .. import balance_schema as bs
from ..clients.balance import CHANGE_SERVICES
from ..deps import Deps

FLOW = "F1"
NORMALIZE = {"Items": bs.item, "Prices": bs.price, "Stocks": bs.stock}


def build_product(row: dict, now: datetime) -> dict:
    """dwh row -> Zoho Products record (only fields owned by the integration)."""
    return {
        "INT_Balance_Code": row["code"] or row["balance_key"],
        "Product_Name": (row["name"] or row["code"] or "")[:120],
        "Unit_Price": float(row["price"]) if row.get("price") is not None else 0.0,
        "Product_Active": bool(row.get("active", True)),
        "INT_Stock": float(row.get("qty") or Decimal(0)),
        "INT_Last_Sync": now.replace(microsecond=0).isoformat(),
    }


def read_changes(d: Deps) -> set[str]:
    """Steps 1–2. Returns balance_keys that changed in this run."""
    changed: set[str] = set()
    for service in CHANGE_SERVICES:
        page = d.balance.changes(service, d.repo.get_cursor(f"balance:{service}"))
        rows = []
        for raw in page.rows:
            r = NORMALIZE[service](raw)
            if not r.get("balance_key"):
                d.repo.log(FLOW, None, "skipped", "row without item key", raw)
                continue
            if service == "Items":
                r["raw"] = raw
            rows.append(r)
        if rows or page.cursor:
            d.repo.save_balance_page(service, rows, page.cursor)
        changed |= {r["balance_key"] for r in rows}
    return changed


def push_products(d: Deps, keys: set[str]) -> tuple[int, int]:
    """Step 3. Returns (ok, failed)."""
    if not keys:
        return 0, 0
    rows = d.repo.product_rows(sorted(keys), d.settings.balance_price_type, d.settings.balance_warehouses)
    products = [build_product(r, d.now()) for r in rows]
    results = d.zoho.upsert_products(products)
    ok = failed = 0
    for prod, res in zip(products, results):
        if res.get("status") == "success":
            ok += 1
        else:
            failed += 1
            d.repo.log(FLOW, prod["INT_Balance_Code"], "failed", res.get("message"), {"record": prod, "result": res})
    return ok, failed


def run(d: Deps) -> dict:
    keys = read_changes(d)
    ok, failed = push_products(d, keys)
    d.repo.log(FLOW, None, "ok", payload={"changed": len(keys), "zoho_ok": ok, "zoho_failed": failed})
    return {"changed": len(keys), "ok": ok, "failed": failed}
