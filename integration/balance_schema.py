"""Mapping from Balance JSON rows to our normalized records.

The exact field names come from the JSON descriptions that Balance generates
(object list form -> "web service description"), stored in docs/balance/*.json.
Until those files arrive, each field is looked up by a list of likely names.
When the real descriptions are in, put the confirmed name FIRST in each list.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# field -> candidate keys (first match wins)
ITEM = {
    "balance_key": ["UUID", "Ref", "GUID", "Id", "ID", "Code"],
    "code":        ["Code", "Article", "Artikul", "SKU"],
    "name":        ["Name", "Description", "FullName"],
    "group_name":  ["Group", "GroupName", "Parent", "Category"],
    "unit":        ["Unit", "UnitName", "BaseUnit"],
    "model":       ["Model", "ModelName"],
    "active":      ["Active", "IsActive"],
    "deleted":     ["DeletionMark", "Deleted", "IsDeleted"],
}
PRICE = {
    "balance_key": ["ItemUUID", "Item", "ItemRef", "ItemCode", "UUID"],
    "price_type":  ["PriceType", "PriceTypeName", "PriceTypeCode"],
    "price":       ["Price", "Value", "Amount"],
    "currency":    ["Currency", "CurrencyCode"],
}
STOCK = {
    "balance_key": ["ItemUUID", "Item", "ItemRef", "ItemCode", "UUID"],
    "warehouse":   ["Warehouse", "WarehouseName", "WarehouseCode", "Store"],
    "qty":         ["Quantity", "Qty", "Balance", "Stock"],
}
CLIENT_BALANCE = {
    "client_code": ["IdentificationCode", "TaxCode", "TIN", "Code", "ClientCode"],
    "client_name": ["Name", "ClientName", "Client"],
    "debt":        ["Debt", "Balance", "Amount", "Sum"],
    "currency":    ["Currency", "CurrencyCode"],
}
SALE = {
    "row_key":     ["RowKey", "LineId", "UUID", "Id"],
    "sale_date":   ["Date", "Period", "DocumentDate"],
    "client_code": ["ClientIdentificationCode", "ClientCode", "IdentificationCode", "TaxCode"],
    "balance_key": ["ItemUUID", "Item", "ItemRef", "ItemCode"],
    "warehouse":   ["Warehouse", "WarehouseName"],
    "qty":         ["Quantity", "Qty"],
    "amount":      ["Amount", "Sum", "Total"],
    "document":    ["Document", "DocumentNumber", "Recorder"],
}


def pick(row: dict, keys: list[str]) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            v = row[k]
            # 1C references often come as {"UUID": ..., "Name": ...}
            if isinstance(v, dict):
                return v.get("UUID") or v.get("Code") or v.get("Name")
            return v
    return None


def to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def to_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "да", "კი"}


def map_row(row: dict, spec: dict[str, list[str]]) -> dict:
    return {f: pick(row, keys) for f, keys in spec.items()}


def item(row: dict) -> dict:
    r = map_row(row, ITEM)
    deleted = to_bool(r.pop("deleted"), default=False)
    r["active"] = to_bool(r["active"], default=True) and not deleted
    r["balance_key"] = str(r["balance_key"]) if r["balance_key"] is not None else None
    r["code"] = str(r["code"]) if r["code"] is not None else r["balance_key"]
    return r


def price(row: dict) -> dict:
    r = map_row(row, PRICE)
    r["price"] = to_decimal(r["price"])
    r["price_type"] = str(r["price_type"] or "")
    return r


def stock(row: dict) -> dict:
    r = map_row(row, STOCK)
    r["qty"] = to_decimal(r["qty"]) or Decimal(0)
    r["warehouse"] = str(r["warehouse"] or "")
    return r


def client_balance(row: dict) -> dict:
    r = map_row(row, CLIENT_BALANCE)
    r["debt"] = to_decimal(r["debt"]) or Decimal(0)
    r["client_code"] = str(r["client_code"]).strip() if r["client_code"] is not None else None
    return r


def sale(row: dict) -> dict:
    r = map_row(row, SALE)
    r["qty"] = to_decimal(r["qty"])
    r["amount"] = to_decimal(r["amount"])
    if r["sale_date"]:
        r["sale_date"] = str(r["sale_date"])[:10]
    if not r["row_key"]:
        # No line id from Balance: build a stable key from the document + item + date.
        r["row_key"] = "|".join(str(r.get(k) or "") for k in ("document", "balance_key", "sale_date", "warehouse"))
    r.pop("document", None)
    return r
