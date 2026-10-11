"""In-memory fakes for the repository and the three external APIs."""
from __future__ import annotations

import itertools
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from integration.clients.balance import Page
from integration.clients.http import ApiError
from integration.config import Settings
from integration.deps import Deps

TZ = ZoneInfo("Asia/Tbilisi")


class FakeRepo:
    def __init__(self):
        self.cursors: dict[str, str] = {}
        self.logs: list[dict] = []
        self.events: dict[tuple, dict] = {}
        self.id_map: list[dict] = []
        self.templates: list[dict] = []
        self.items: dict[str, dict] = {}
        self.prices: dict[tuple, dict] = {}
        self.stock: dict[tuple, dict] = {}
        self.client_balances: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.deals: list[dict] = []
        self.tasks: list[dict] = []
        self.sales: dict[str, dict] = {}

    # sync_state
    def get_cursor(self, s): return self.cursors.get(s)
    def set_cursor(self, s, v, conn=None): self.cursors[s] = v
    def clear_cursor(self, s): self.cursors.pop(s, None)

    # sync_log
    def log(self, flow, ref, status, error=None, payload=None, attempts=1):
        self.logs.append(dict(flow=flow, entity_ref=ref, status=status, error=error, payload=payload))
    def recent_log(self, limit=100, flow=None): return self.logs[-limit:]

    # inbound
    def insert_event(self, source, key, flow, payload):
        if (source, key) in self.events:
            return False
        self.events[(source, key)] = dict(source=source, event_key=key, flow=flow, payload=payload,
                                          processed=False, attempts=0)
        return True
    def event_done(self, s, k): self.events[(s, k)]["processed"] = True
    def event_failed(self, s, k, e): self.events[(s, k)]["attempts"] += 1
    def pending_events(self, **kw): return [e for e in self.events.values() if not e["processed"]]

    # id_map
    def id_exists(self, e, z, t): return self.id_get(e, z, t) is not None
    def id_get(self, e, z, t):
        return next((r["clickup_id"] for r in self.id_map
                     if r["entity"] == e and r["zoho_id"] == z and r["template_key"] == t), None)
    def id_add(self, e, z, c, t):
        if not self.id_exists(e, z, t):
            self.id_map.append(dict(entity=e, zoho_id=z, clickup_id=c, template_key=t))
    def tasks_of(self, e, z): return [r["clickup_id"] for r in self.id_map if r["entity"] == e and r["zoho_id"] == z]
    def owner_of(self, e, c):
        r = next((r for r in self.id_map if r["entity"] == e and r["clickup_id"] == c), None)
        return (r["zoho_id"], r["template_key"]) if r else None

    def templates_for_stage(self, st): return [t for t in self.templates if t["trigger_stage"] == st]

    # dwh
    def save_balance_page(self, service, rows, cursor):
        for r in rows:
            if service == "Items":
                self.items[r["balance_key"]] = r
            elif service == "Prices":
                self.prices[(r["balance_key"], r["price_type"])] = r
            else:
                self.stock[(r["balance_key"], r["warehouse"])] = r
        if cursor:
            self.cursors[f"balance:{service}"] = cursor

    def product_rows(self, keys, price_type, warehouses):
        out = []
        for k in keys:
            i = self.items.get(k)
            if not i:
                continue
            price = next((p["price"] for (bk, pt), p in self.prices.items()
                          if bk == k and (not price_type or pt == price_type)), None)
            qty = sum((s["qty"] for (bk, wh), s in self.stock.items()
                       if bk == k and (not warehouses or wh in warehouses)), Decimal(0))
            out.append(dict(balance_key=k, code=i["code"], name=i["name"], active=i["active"], price=price, qty=qty))
        return out

    def keys_for_codes(self, codes): return [k for k, i in self.items.items() if i["code"] in codes or k in codes]
    def client_debt(self, code):
        r = self.client_balances.get(code)
        return r["debt"] if r else None

    def upsert_client_balances(self, rows):
        changed = []
        for r in rows:
            old = self.client_balances.get(r["client_code"])
            self.client_balances[r["client_code"]] = r
            if old is None or old["debt"] != r["debt"]:
                changed.append(r)
        return changed

    def upsert_sales(self, rows):
        for r in rows:
            self.sales[r["row_key"]] = r
        return len(rows)

    def snapshot_stock(self): return len(self.stock)
    def upsert_deals(self, rows, cursor):
        self.deals.extend(rows)
        if cursor:
            self.cursors["zoho:Deals"] = cursor
    def upsert_tasks(self, rows, cursor):
        self.tasks.extend(rows)
        if cursor:
            self.cursors["clickup:tasks"] = cursor
    def upsert_supplier_payment(self, r): self.payments[r["clickup_id"]] = r
    def payments_to_remind(self, today): return [p for p in self.payments.values()
                                                 if not p["paid"] and p["remind_date"] == today]
    def overdue_payments(self, today): return [p for p in self.payments.values()
                                               if not p["paid"] and p["due_date"] and p["due_date"] < today]


class FakeZoho:
    def __init__(self):
        self.deals: dict[str, dict] = {}
        self.deal_updates: list[tuple] = []
        self.accounts: dict[str, dict] = {}       # ident -> account
        self.account_updates: list[tuple] = []
        self.products: list[dict] = []
        self.fail_codes: set[str] = set()

    def get_deal(self, deal_id):
        if deal_id not in self.deals:
            raise ApiError("zoho", 404, "not found")
        return self.deals[deal_id]

    def update_deal(self, deal_id, fields):
        assert all(k.startswith("INT_") for k in fields), fields
        self.deal_updates.append((deal_id, fields))
        self.deals.setdefault(deal_id, {"id": deal_id}).update(fields)
        return {"status": "success"}

    def unpaid_closed_deals(self, account_id):
        return [d for d in self.deals.values()
                if (d.get("Account_Name") or {}).get("id") == account_id
                and d.get("Stage") == "Closed Deal" and not d.get("INT_Paid")]

    def upsert_products(self, rows):
        self.products.extend(rows)
        return [{"status": "error", "message": "boom"} if r["INT_Balance_Code"] in self.fail_codes
                else {"status": "success"} for r in rows]

    def find_account_by_ident(self, ident): return self.accounts.get(ident)
    def update_account(self, account_id, fields):
        assert all(k.startswith("INT_") for k in fields), fields
        self.account_updates.append((account_id, fields))
        return {"status": "success"}
    def account_codes(self, ids):
        return {a["id"]: code for code, a in self.accounts.items() if a["id"] in ids}
    def coql(self, q): return []


class FakeClickUp:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.created: list[tuple] = []
        self.updated: list[tuple] = []
        self.fields_set: list[tuple] = []
        self.comments: list[tuple] = []
        self._ids = (f"t{i}" for i in itertools.count(1))

    def get_task(self, task_id):
        if task_id not in self.tasks:
            raise ApiError("clickup", 404, "not found")
        t = dict(self.tasks[task_id])
        t["subtasks"] = [dict(s) for s in self.tasks.values() if s.get("parent") == task_id]
        return t

    def create_task(self, list_id, body):
        tid = next(self._ids)
        task = {"id": tid, "list": {"id": list_id}, "status": {"status": "to do", "type": "open"},
                "custom_fields": [{"id": "cf_deal", "name": "Zoho Deal ID"},
                                  {"id": "cf_url", "name": "Zoho Deal URL"},
                                  {"id": "cf_tpl", "name": "Template"},
                                  {"id": "cf_paid", "name": "გადახდილია"}],
                **{k: (str(v) if k in ("due_date", "start_date") else v) for k, v in body.items()}}
        self.tasks[tid] = task
        self.created.append((list_id, body))
        return dict(task)

    def update_task(self, task_id, body):
        self.updated.append((task_id, body))
        self.tasks[task_id].update({k: (str(v) if k in ("due_date", "start_date") else v) for k, v in body.items()})
        return self.tasks[task_id]

    def set_field(self, task_id, fid, value):
        self.fields_set.append((task_id, fid, value))
        for f in self.tasks[task_id].get("custom_fields", []):
            if f["id"] == fid:
                f["value"] = value

    def comment(self, task_id, text, notify_all=False): self.comments.append((task_id, text))
    def tasks_updated_since(self, since, spaces=None): return []

    def close(self, task_id, closed_on: date):
        self.tasks[task_id]["status"] = {"status": "დახურული", "type": "closed"}
        self.tasks[task_id]["date_closed"] = str(int(datetime(closed_on.year, closed_on.month, closed_on.day,
                                                              12, tzinfo=TZ).timestamp() * 1000))


class FakeBalance:
    def __init__(self):
        self.pages: dict[str, Page] = {}
        self.full_rows: dict[str, list] = {}
        self.sales: list[dict] = []

    def changes(self, service, last):
        return self.pages.pop(service, Page([], None))

    def full(self, service, params=None): return self.full_rows.get(service, [])
    def sales_report(self, a, b): return [r for r in self.sales if a.isoformat() <= r["Date"][:10] <= b.isoformat()]


def make_deps(today: date = date(2026, 10, 12), **overrides) -> Deps:
    s = Settings()
    object.__setattr__(s, "clickup_contracts_list_id", "L_CONTRACTS")
    object.__setattr__(s, "clickup_service_user_id", "999")
    object.__setattr__(s, "inbound_token", "tok")
    object.__setattr__(s, "clickup_webhook_secret", "sec")
    object.__setattr__(s, "clickup_contracts_webhook_secret", "sec2")
    object.__setattr__(s, "run_scheduler", False)
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return Deps(repo=FakeRepo(), zoho=FakeZoho(), clickup=FakeClickUp(), balance=FakeBalance(), settings=s,
                now=lambda: datetime(today.year, today.month, today.day, 10, 0, tzinfo=TZ))


def ms_of(d: date) -> str:
    return str(int(datetime(d.year, d.month, d.day, 9, tzinfo=TZ).timestamp() * 1000))
