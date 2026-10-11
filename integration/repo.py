"""Repository: every SQL statement the flows need lives here.

Flows depend on this class's methods only, so tests can swap in an
in-memory fake (tests/fakes.py) with the same interface.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any

from . import db

log = logging.getLogger(__name__)

SECRET_KEYS = {"access_token", "refresh_token", "client_secret", "password", "token", "authorization", "secret"}


def redact(obj: Any) -> Any:
    """Strip secrets before anything is written to ops.sync_log."""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in SECRET_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, (Decimal, date)):
        return str(obj)
    return obj


class Repo:
    # ================= ops.sync_state =================
    def get_cursor(self, source: str) -> str | None:
        r = db.fetch_one("SELECT cursor_value FROM ops.sync_state WHERE source = %s", (source,))
        return r["cursor_value"] if r else None

    def set_cursor(self, source: str, value: str | None, conn=None) -> None:
        sql = ("INSERT INTO ops.sync_state (source, cursor_value, updated_at) VALUES (%s, %s, now()) "
               "ON CONFLICT (source) DO UPDATE SET cursor_value = EXCLUDED.cursor_value, updated_at = now()")
        if conn is not None:
            conn.execute(sql, (source, value))
        else:
            db.execute(sql, (source, value))

    def clear_cursor(self, source: str) -> None:
        db.execute("DELETE FROM ops.sync_state WHERE source = %s", (source,))

    # ================= ops.sync_log =================
    def log(self, flow: str, ref: str | None, status: str, error: str | None = None,
            payload: Any = None, attempts: int = 1) -> None:
        try:
            db.execute(
                "INSERT INTO ops.sync_log (flow, entity_ref, status, attempts, error, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (flow, ref, status, attempts, (error or "")[:2000] or None,
                 db.json_param(redact(payload)) if payload is not None else None),
            )
        except Exception:  # noqa: BLE001 — logging must never break a flow
            log.exception("could not write sync_log")

    def recent_log(self, limit: int = 100, flow: str | None = None) -> list[dict]:
        if flow:
            return db.fetch_all("SELECT * FROM ops.sync_log WHERE flow = %s ORDER BY id DESC LIMIT %s",
                                (flow, limit))
        return db.fetch_all("SELECT * FROM ops.sync_log ORDER BY id DESC LIMIT %s", (limit,))

    def failed_to_retry(self, limit: int = 50) -> list[dict]:
        """Failed rows from the last 24 h not retried yet (retry_failed: every 10 min, max 24 tries)."""
        return db.fetch_all(
            "SELECT * FROM ops.sync_log WHERE status = 'failed' AND retried_at IS NULL "
            "AND attempts < 24 AND created_at > now() - interval '24 hours' ORDER BY id LIMIT %s",
            (limit,))

    def mark_retried(self, log_id: int) -> None:
        db.execute("UPDATE ops.sync_log SET retried_at = now() WHERE id = %s", (log_id,))

    def alert_stats(self) -> dict:
        return db.fetch_one(
            "SELECT (SELECT count(*) FROM ops.sync_log WHERE status = 'failed' "
            "        AND created_at > now() - interval '1 hour') AS failed_1h, "
            "       (SELECT max(created_at) FROM ops.sync_log WHERE flow = 'F1' AND status = 'ok') AS last_f1_ok")

    def purge_log(self, days: int = 90) -> int:
        n = db.execute("DELETE FROM ops.sync_log WHERE created_at < now() - make_interval(days => %s::int)", (days,))
        db.execute("DELETE FROM ops.inbound_event WHERE processed_at < now() - make_interval(days => %s::int)", (days,))
        return n

    # ================= ops.inbound_event =================
    def insert_event(self, source: str, key: str, flow: str, payload: dict) -> bool:
        """True if the event is new; False if this exact event was already received."""
        r = db.fetch_one(
            "INSERT INTO ops.inbound_event (source, event_key, flow, payload) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING event_key",
            (source, key, flow, db.json_param(payload)))
        return r is not None

    def event_done(self, source: str, key: str) -> None:
        db.execute("UPDATE ops.inbound_event SET processed_at = now(), last_error = NULL "
                   "WHERE source = %s AND event_key = %s", (source, key))

    def event_failed(self, source: str, key: str, error: str) -> None:
        db.execute("UPDATE ops.inbound_event SET attempts = attempts + 1, last_error = %s "
                   "WHERE source = %s AND event_key = %s", (error[:2000], source, key))

    def pending_events(self, older_than_s: int = 60, max_attempts: int = 24, limit: int = 50) -> list[dict]:
        return db.fetch_all(
            "SELECT * FROM ops.inbound_event WHERE processed_at IS NULL AND attempts < %s "
            "AND received_at < now() - make_interval(secs => %s::int) ORDER BY received_at LIMIT %s",
            (max_attempts, older_than_s, limit))

    # ================= ops.id_map =================
    def id_exists(self, entity: str, zoho_id: str, template_key: str) -> bool:
        return db.fetch_one("SELECT 1 FROM ops.id_map WHERE entity=%s AND zoho_id=%s AND template_key=%s",
                            (entity, zoho_id, template_key)) is not None

    def id_get(self, entity: str, zoho_id: str, template_key: str) -> str | None:
        r = db.fetch_one("SELECT clickup_id FROM ops.id_map WHERE entity=%s AND zoho_id=%s AND template_key=%s",
                         (entity, zoho_id, template_key))
        return r["clickup_id"] if r else None

    def id_add(self, entity: str, zoho_id: str, clickup_id: str, template_key: str) -> None:
        db.execute("INSERT INTO ops.id_map (entity, zoho_id, clickup_id, template_key) VALUES (%s,%s,%s,%s) "
                   "ON CONFLICT DO NOTHING", (entity, zoho_id, clickup_id, template_key))

    def tasks_of(self, entity: str, zoho_id: str) -> list[str]:
        return [r["clickup_id"] for r in db.fetch_all(
            "SELECT clickup_id FROM ops.id_map WHERE entity=%s AND zoho_id=%s ORDER BY id", (entity, zoho_id))]

    def owner_of(self, entity: str, clickup_id: str) -> tuple[str, str] | None:
        """(zoho_id, template_key) that owns a ClickUp task."""
        r = db.fetch_one("SELECT zoho_id, template_key FROM ops.id_map WHERE entity=%s AND clickup_id=%s",
                         (entity, clickup_id))
        return (r["zoho_id"], r["template_key"]) if r else None

    # ================= ops.task_template =================
    def templates_for_stage(self, stage: str) -> list[dict]:
        return db.fetch_all("SELECT * FROM ops.task_template WHERE trigger_stage=%s AND active ORDER BY template_key",
                            (stage,))

    # ================= dwh: Balance =================
    def save_balance_page(self, service: str, rows: list[dict], cursor: str | None) -> None:
        """Upsert normalized rows AND move the cursor in ONE transaction.

        If the upsert fails the cursor stays where it was, so the same
        changes are read again next run (nothing is lost)."""
        with db.tx() as c:
            if service == "Items":
                for r in rows:
                    c.execute(
                        "INSERT INTO dwh.item (balance_key, code, name, group_name, unit, model, active, raw, updated_at) "
                        "VALUES (%(balance_key)s, %(code)s, %(name)s, %(group_name)s, %(unit)s, %(model)s, %(active)s, %(raw)s, now()) "
                        "ON CONFLICT (balance_key) DO UPDATE SET code=EXCLUDED.code, name=EXCLUDED.name, "
                        "group_name=EXCLUDED.group_name, unit=EXCLUDED.unit, model=EXCLUDED.model, "
                        "active=EXCLUDED.active, raw=EXCLUDED.raw, updated_at=now()",
                        {**r, "raw": db.json_param(r.get("raw"))})
            elif service == "Prices":
                for r in rows:
                    c.execute(
                        "INSERT INTO dwh.price (balance_key, price_type, price, currency, updated_at) "
                        "VALUES (%(balance_key)s, %(price_type)s, %(price)s, %(currency)s, now()) "
                        "ON CONFLICT (balance_key, price_type) DO UPDATE SET price=EXCLUDED.price, "
                        "currency=EXCLUDED.currency, updated_at=now()", r)
            elif service == "Stocks":
                for r in rows:
                    c.execute(
                        "INSERT INTO dwh.stock (balance_key, warehouse, qty, updated_at) "
                        "VALUES (%(balance_key)s, %(warehouse)s, %(qty)s, now()) "
                        "ON CONFLICT (balance_key, warehouse) DO UPDATE SET qty=EXCLUDED.qty, updated_at=now()", r)
            else:
                raise ValueError(service)
            if cursor:
                self.set_cursor(f"balance:{service}", cursor, conn=c)

    def product_rows(self, keys: list[str], price_type: str, warehouses: list[str]) -> list[dict]:
        """Everything F1 needs to build Zoho Products for the changed items."""
        return db.fetch_all(
            "SELECT i.balance_key, i.code, i.name, i.active, "
            "       (SELECT price FROM dwh.price p WHERE p.balance_key = i.balance_key "
            "         AND (%(pt)s = '' OR p.price_type = %(pt)s) ORDER BY updated_at DESC LIMIT 1) AS price, "
            "       (SELECT coalesce(sum(qty), 0) FROM dwh.stock s WHERE s.balance_key = i.balance_key "
            "         AND (cardinality(%(wh)s::text[]) = 0 OR s.warehouse = ANY(%(wh)s::text[]))) AS qty "
            "FROM dwh.item i WHERE i.balance_key = ANY(%(keys)s::text[])",
            {"pt": price_type or "", "wh": warehouses, "keys": keys})

    def keys_for_codes(self, codes: list[str]) -> list[str]:
        return [r["balance_key"] for r in db.fetch_all(
            "SELECT balance_key FROM dwh.item WHERE code = ANY(%s::text[]) OR balance_key = ANY(%s::text[])",
            (codes, codes))]

    def client_debt(self, client_code: str) -> Decimal | None:
        r = db.fetch_one("SELECT debt FROM dwh.client_balance WHERE client_code = %s", (client_code,))
        return Decimal(r["debt"]) if r else None

    def upsert_client_balances(self, rows: list[dict]) -> list[dict]:
        """Upsert debts; return only rows whose debt actually changed (or are new)."""
        changed = []
        with db.tx() as c:
            for r in rows:
                if not r.get("client_code"):
                    continue
                old = c.execute("SELECT debt FROM dwh.client_balance WHERE client_code=%s",
                                (r["client_code"],)).fetchone()
                c.execute(
                    "INSERT INTO dwh.client_balance (client_code, client_name, debt, currency, updated_at) "
                    "VALUES (%(client_code)s, %(client_name)s, %(debt)s, %(currency)s, now()) "
                    "ON CONFLICT (client_code) DO UPDATE SET client_name=EXCLUDED.client_name, "
                    "debt=EXCLUDED.debt, currency=EXCLUDED.currency, updated_at=now()", r)
                if old is None or Decimal(old["debt"]) != Decimal(r["debt"]):
                    changed.append(r)
        return changed

    def upsert_sales(self, rows: list[dict]) -> int:
        with db.tx() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO dwh.sale (row_key, sale_date, client_code, balance_key, warehouse, qty, amount, raw) "
                    "VALUES (%(row_key)s, %(sale_date)s, %(client_code)s, %(balance_key)s, %(warehouse)s, "
                    "%(qty)s, %(amount)s, %(raw)s) ON CONFLICT (row_key) DO UPDATE SET "
                    "sale_date=EXCLUDED.sale_date, client_code=EXCLUDED.client_code, balance_key=EXCLUDED.balance_key, "
                    "warehouse=EXCLUDED.warehouse, qty=EXCLUDED.qty, amount=EXCLUDED.amount, raw=EXCLUDED.raw",
                    {**r, "raw": db.json_param(r.get("raw"))})
        return len(rows)

    def snapshot_stock(self) -> int:
        return db.execute(
            "INSERT INTO dwh.stock_snapshot (snap_date, balance_key, warehouse, qty) "
            "SELECT current_date, balance_key, warehouse, qty FROM dwh.stock ON CONFLICT DO NOTHING")

    # ================= dwh: Zoho / ClickUp =================
    def upsert_deals(self, rows: list[dict], cursor: str | None) -> None:
        with db.tx() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO dwh.deal (zoho_id, name, stage, amount, account_id, account_code, owner, win_code, "
                    "closing_date, paid, exec_status, created_time, modified_time) VALUES (%(zoho_id)s, %(name)s, "
                    "%(stage)s, %(amount)s, %(account_id)s, %(account_code)s, %(owner)s, %(win_code)s, %(closing_date)s, "
                    "%(paid)s, %(exec_status)s, %(created_time)s, %(modified_time)s) ON CONFLICT (zoho_id) DO UPDATE SET "
                    "name=EXCLUDED.name, stage=EXCLUDED.stage, amount=EXCLUDED.amount, account_id=EXCLUDED.account_id, "
                    "account_code=EXCLUDED.account_code, owner=EXCLUDED.owner, win_code=EXCLUDED.win_code, "
                    "closing_date=EXCLUDED.closing_date, paid=EXCLUDED.paid, exec_status=EXCLUDED.exec_status, "
                    "created_time=EXCLUDED.created_time, modified_time=EXCLUDED.modified_time", r)
            if cursor:
                self.set_cursor("zoho:Deals", cursor, conn=c)

    def upsert_tasks(self, rows: list[dict], cursor: str | None) -> None:
        with db.tx() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO dwh.task (clickup_id, zoho_deal_id, template_key, name, status, status_type, assignees, "
                    "date_created, date_done, due_date) VALUES (%(clickup_id)s, %(zoho_deal_id)s, %(template_key)s, "
                    "%(name)s, %(status)s, %(status_type)s, %(assignees)s, %(date_created)s, %(date_done)s, %(due_date)s) "
                    "ON CONFLICT (clickup_id) DO UPDATE SET zoho_deal_id=EXCLUDED.zoho_deal_id, "
                    "template_key=EXCLUDED.template_key, name=EXCLUDED.name, status=EXCLUDED.status, "
                    "status_type=EXCLUDED.status_type, assignees=EXCLUDED.assignees, date_created=EXCLUDED.date_created, "
                    "date_done=EXCLUDED.date_done, due_date=EXCLUDED.due_date", r)
            if cursor:
                self.set_cursor("clickup:tasks", cursor, conn=c)

    def payments_to_remind(self, today: date) -> list[dict]:
        """Unpaid payments whose reminder day is today (or was missed in the last 3 days)."""
        return db.fetch_all(
            "SELECT * FROM dwh.supplier_payment WHERE NOT paid AND remind_date BETWEEN %s::date - 3 AND %s::date",
            (today, today))

    def overdue_payments(self, today: date) -> list[dict]:
        return db.fetch_all("SELECT * FROM dwh.supplier_payment WHERE NOT paid AND due_date < %s::date", (today,))

    def upsert_supplier_payment(self, r: dict) -> None:
        db.execute(
            "INSERT INTO dwh.supplier_payment (clickup_id, contract_id, contract_no, supplier, step, amount, currency, "
            "due_date, remind_date, paid, paid_date, updated_at) VALUES (%(clickup_id)s, %(contract_id)s, %(contract_no)s, "
            "%(supplier)s, %(step)s, %(amount)s, %(currency)s, %(due_date)s, %(remind_date)s, %(paid)s, %(paid_date)s, now()) "
            "ON CONFLICT (clickup_id) DO UPDATE SET contract_no=EXCLUDED.contract_no, supplier=EXCLUDED.supplier, "
            "amount=EXCLUDED.amount, currency=EXCLUDED.currency, due_date=EXCLUDED.due_date, remind_date=EXCLUDED.remind_date, paid=EXCLUDED.paid, "
            "paid_date=EXCLUDED.paid_date, updated_at=now()", r)


def dumps(obj: Any) -> str:
    return json.dumps(redact(obj), ensure_ascii=False, default=str)
