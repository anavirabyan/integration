"""F5 — loads for Power BI.

Hourly : Zoho deals (COQL, Modified_Time > cursor) -> dwh.deal
         ClickUp execution tasks (date_updated > cursor) -> dwh.task
Nightly: Balance SalesReport for yesterday -> dwh.sale
         copy of dwh.stock -> dwh.stock_snapshot
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import balance_schema as bs
from ..clients.clickup import field_value, from_ms_dt
from ..clients.zoho import _q
from ..deps import Deps

PAGE = 200
DEAL_FIELDS = ("id, Deal_Name, Stage, Amount, Account_Name, Owner, Closing_Date, "
               "INT_Paid, INT_Execution_Status, Created_Time, Modified_Time")


def load_zoho_deals(d: Deps) -> int:
    cursor = d.repo.get_cursor("zoho:Deals") or "2000-01-01T00:00:00+00:00"
    total = 0
    while True:
        q = (f"select {DEAL_FIELDS}, {d.settings.zoho_field_win} from Deals "
             f"where Modified_Time > '{_q(cursor)}' order by Modified_Time asc limit {PAGE}")
        rows = d.zoho.coql(q)
        if not rows:
            break
        codes = d.zoho.account_codes([(r.get("Account_Name") or {}).get("id") for r in rows
                                      if (r.get("Account_Name") or {}).get("id")])
        mapped = [map_deal(r, codes, d.settings.zoho_field_win) for r in rows]
        cursor = rows[-1]["Modified_Time"]
        d.repo.upsert_deals(mapped, cursor)
        total += len(rows)
        if len(rows) < PAGE:
            break
    d.repo.log("F5", "zoho:Deals", "ok", payload={"rows": total})
    return total


def map_deal(r: dict, codes: dict[str, str], win_field: str) -> dict:
    acc = r.get("Account_Name") or {}
    owner = r.get("Owner") or {}
    return {
        "zoho_id": r["id"],
        "name": r.get("Deal_Name"),
        "stage": r.get("Stage"),
        "amount": r.get("Amount"),
        "account_id": acc.get("id"),
        "account_code": codes.get(acc.get("id")),
        "owner": owner.get("name") or owner.get("email"),
        "win_code": r.get(win_field),
        "closing_date": r.get("Closing_Date"),
        "paid": r.get("INT_Paid"),
        "exec_status": r.get("INT_Execution_Status"),
        "created_time": r.get("Created_Time"),
        "modified_time": r.get("Modified_Time"),
    }


def load_clickup_tasks(d: Deps) -> int:
    cursor = d.repo.get_cursor("clickup:tasks")
    since = int(cursor) if cursor else 0
    spaces = [d.settings.clickup_execution_space_id] if d.settings.clickup_execution_space_id else None
    tasks = d.clickup.tasks_updated_since(since, spaces)
    rows = [map_task(t) for t in tasks]
    newest = max([int(t.get("date_updated") or 0) for t in tasks] + [since])
    d.repo.upsert_tasks(rows, str(newest) if newest else None)
    d.repo.log("F5", "clickup:tasks", "ok", payload={"rows": len(rows)})
    return len(rows)


def map_task(t: dict) -> dict:
    st = t.get("status") or {}
    return {
        "clickup_id": t["id"],
        "zoho_deal_id": field_value(t, "Zoho Deal ID"),
        "template_key": field_value(t, "Template"),
        "name": t.get("name"),
        "status": st.get("status"),
        "status_type": st.get("type"),
        "assignees": [str(a.get("id")) for a in t.get("assignees") or []],
        "date_created": from_ms_dt(t.get("date_created")),
        "date_done": from_ms_dt(t.get("date_done") or t.get("date_closed")),
        "due_date": from_ms_dt(t.get("due_date")),
    }


def load_sales_report(d: Deps, date_from: date | None = None, date_to: date | None = None) -> int:
    """Default: yesterday. Backfill passes a range (e.g. from 2024-01-01 for reports 4 and 6)."""
    yesterday = d.today() - timedelta(days=1)
    date_from = date_from or yesterday
    date_to = date_to or yesterday
    total = 0
    # Month by month, so a long backfill never sends one huge request.
    start = date_from
    while start <= date_to:
        end = min(_month_end(start), date_to)
        rows = []
        for raw in d.balance.sales_report(start, end):
            r = bs.sale(raw)
            r["raw"] = raw
            if r["sale_date"]:
                rows.append(r)
        total += d.repo.upsert_sales(rows)
        start = end + timedelta(days=1)
    d.repo.log("F5", "balance:SalesReport", "ok", payload={"rows": total, "from": date_from, "to": date_to})
    return total


def snapshot_stock(d: Deps) -> int:
    n = d.repo.snapshot_stock()
    d.repo.log("F5", "stock_snapshot", "ok", payload={"rows": n})
    return n


def _month_end(day: date) -> date:
    nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)
