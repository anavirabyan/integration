"""Balance (1C) Exchange web services — READ ONLY.

Hard rule: this module only ever sends GET. There is deliberately no generic
request method here, so nobody can call POST/PUT/PATCH/DELETE through it.
Code review must reject any change that adds a write method.

Base URL:
  cloud: https://cloud.balance.ge/sm/o/Balance/<ApplicationID>/hs/Exchange
  local: http://<ServerIP>/Balance_Exchange/hs/Exchange
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import settings
from .http import HttpClient

# Services that support incremental reads via LastReadChange.
CHANGE_SERVICES = ("Items", "Prices", "Stocks")


@dataclass
class Page:
    rows: list[dict]
    cursor: str | None  # last change number from the response header


class BalanceClient:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient(
            "balance", settings.balance_base_url, timeout=60,
            basic_auth=(settings.balance_user, settings.balance_pass),
        )

    def _get(self, service: str, params: dict | None = None) -> tuple[Any, dict]:
        resp = self.http.request("GET", f"/{service}", params=params or {})
        try:
            body = resp.json() if resp.content else []
        except ValueError:
            body = []
        return body, resp.headers

    def changes(self, service: str, last_change: str | None) -> Page:
        """Items / Prices / Stocks — only rows changed after `last_change`.

        Without LastReadChange Balance returns everything (used by backfill).
        With no changes it returns an empty array.
        """
        if service not in CHANGE_SERVICES:
            raise ValueError(f"{service} does not support change reads")
        params = {"ChangeNodeCode": settings.balance_node_code}
        if last_change:
            params["LastReadChange"] = last_change
        body, headers = self._get(service, params)
        return Page(rows=_rows(body), cursor=headers.get(settings.balance_change_header))

    def full(self, service: str, params: dict | None = None) -> list[dict]:
        """Full read: PriceTypes, Warehouses, ClientsBalances."""
        body, _ = self._get(service, params)
        return _rows(body)

    def sales_report(self, date_from: date, date_to: date) -> list[dict]:
        """Sales register for a period. Parameter names are confirmed against
        the SalesReport JSON description (docs/balance/SalesReport.json)."""
        body, _ = self._get("SalesReport", {
            "DateFrom": date_from.isoformat(),
            "DateTo": date_to.isoformat(),
        })
        return _rows(body)


def _rows(body: Any) -> list[dict]:
    """Balance may return a bare array or an object wrapping one."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "Data", "items", "Items", "rows", "Rows", "result", "Result"):
            if isinstance(body.get(key), list):
                return body[key]
    return []
