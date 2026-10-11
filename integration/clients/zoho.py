"""Zoho CRM REST client (Self Client + refresh token).

The middleware writes ONLY INT_* fields. It never changes Stage or any field
that belongs to the Gegidze Consulting configuration — `update_deal` enforces this.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable

import requests

from ..config import settings
from .http import ApiError, HttpClient

BATCH = 100  # Zoho limit: max 100 records per insert / update / upsert call


class ZohoAuth:
    """refresh_token -> access_token (~1 h), cached in memory, thread-safe."""

    def __init__(self, session: requests.Session | None = None):
        self._token: str | None = None
        self._exp = 0.0
        self._lock = threading.Lock()
        self._session = session or requests.Session()

    def header(self) -> dict:
        with self._lock:
            if not self._token or time.time() > self._exp - 60:
                self._refresh()
            return {"Authorization": f"Zoho-oauthtoken {self._token}"}

    def invalidate(self) -> None:
        with self._lock:
            self._token, self._exp = None, 0.0

    def _refresh(self) -> None:
        r = self._session.post(settings.zoho_accounts_url, params={
            "refresh_token": settings.zoho_refresh_token,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        }, timeout=30)
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "access_token" not in data:
            # Never log the response body: it may echo secrets.
            raise ApiError("zoho-auth", r.status_code, data.get("error", "token refresh failed"))
        self._token = data["access_token"]
        self._exp = time.time() + int(data.get("expires_in", 3600))


def batched(items: list, n: int = BATCH) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


class ZohoClient:
    def __init__(self, http: HttpClient | None = None, auth: ZohoAuth | None = None):
        self.auth = auth or ZohoAuth()
        self.http = http or HttpClient(
            "zoho", settings.zoho_api_base, timeout=30,
            auth_header=self.auth.header, on_unauthorized=self.auth.invalidate,
        )

    # ---------- Deals ----------
    def get_deal(self, deal_id: str) -> dict:
        data = self.http.get(f"/Deals/{deal_id}").get("data") or []
        if not data:
            raise ApiError("zoho", 404, f"deal {deal_id} not found")
        return data[0]

    def update_deal(self, deal_id: str, fields: dict) -> dict:
        bad = [k for k in fields if not k.startswith("INT_")]
        if bad:
            raise ValueError(f"middleware may only write INT_* fields, got {bad}")
        return self._check(self.http.put("/Deals", json={"data": [{"id": deal_id, **fields}]}))[0]

    def unpaid_closed_deals(self, account_id: str) -> list[dict]:
        q = ("select id, Deal_Name from Deals "
             f"where Account_Name = '{_q(account_id)}' and Stage = '{_q(settings.stage_closed)}' "
             "and (INT_Paid = false or INT_Paid is null) limit 200")
        return self.coql(q)

    # ---------- Products ----------
    def upsert_products(self, rows: list[dict]) -> list[dict]:
        """Upsert by INT_Balance_Code. Returns one result per row, in order."""
        results: list[dict] = []
        for chunk in batched(rows):
            resp = self.http.post("/Products/upsert", idempotent=True, json={
                "data": chunk,
                "duplicate_check_fields": ["INT_Balance_Code"],
            })
            results.extend(resp.get("data") or [])
        return results

    # ---------- Accounts ----------
    def find_account_by_ident(self, ident: str) -> dict | None:
        crit = f"({settings.zoho_field_ident}:equals:{ident})"
        data = self.http.get("/Accounts/search", params={"criteria": crit}).get("data") or []
        return data[0] if data else None

    def update_account(self, account_id: str, fields: dict) -> dict:
        bad = [k for k in fields if not k.startswith("INT_")]
        if bad:
            raise ValueError(f"middleware may only write INT_* fields, got {bad}")
        return self._check(self.http.put("/Accounts", json={"data": [{"id": account_id, **fields}]}))[0]

    def account_codes(self, account_ids: list[str]) -> dict[str, str]:
        """{account_id: Identification_Code} for a set of accounts."""
        out: dict[str, str] = {}
        for chunk in batched(sorted(set(account_ids)), 50):
            ids = ",".join(f"'{_q(i)}'" for i in chunk)
            q = f"select id, {settings.zoho_field_ident} from Accounts where id in ({ids})"
            for r in self.coql(q):
                if r.get(settings.zoho_field_ident):
                    out[r["id"]] = str(r[settings.zoho_field_ident])
        return out

    # ---------- COQL ----------
    def coql(self, query: str) -> list[dict]:
        resp = self.http.post("/coql", idempotent=True, json={"select_query": query})
        return resp.get("data") or []

    @staticmethod
    def _check(resp: dict) -> list[dict]:
        data = resp.get("data") or []
        errors = [d for d in data if d.get("status") != "success"]
        if errors:
            raise ApiError("zoho", 400, f"record error: {errors[0].get('message')}", errors)
        return data


def deal_url(deal_id: str) -> str:
    return f"{settings.zoho_org_url.rstrip('/')}/tab/Potentials/{deal_id}"


def _q(value: str) -> str:
    """Escape a value for a COQL string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")
