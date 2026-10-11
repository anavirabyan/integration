"""F2 — client debts from Balance -> Zoho Accounts / Deals + ClickUp. Every 30 min.

1. Read ClientsBalances (full) -> dwh.client_balance; keep only changed debts.
2. Find the Zoho Account by Identification_Code -> write INT_Balance_Debt.
3. Debt <= 0 -> every Closed Deal of that account with INT_Paid = false becomes
   INT_Paid = true, and the "გადახდილია" checkbox is set on its ClickUp tasks.

Limitation: Balance records debt per client, not per deal. With several open
deals per client a single deal's payment cannot be told apart; until accounting
can link debt to a deal, "debt <= 0" marks all of the client's closed deals paid.
Flags only ever go false -> true (idempotent, never un-pays a deal).
"""
from __future__ import annotations

from .. import balance_schema as bs
from ..clients.clickup import field_id
from ..deps import Deps

FLOW = "F2"
PAID_FIELD = "გადახდილია"


def run(d: Deps) -> dict:
    rows = [bs.client_balance(r) for r in d.balance.full("ClientsBalances")]
    changed = d.repo.upsert_client_balances(rows)
    accounts = deals_paid = 0
    for c in changed:
        try:
            acc = d.zoho.find_account_by_ident(c["client_code"])
            if not acc:
                d.repo.log(FLOW, c["client_code"], "skipped", "account not found in Zoho")
                continue
            d.zoho.update_account(acc["id"], {"INT_Balance_Debt": float(c["debt"])})
            accounts += 1
            if c["debt"] <= 0:
                deals_paid += mark_paid(d, acc["id"])
        except Exception as exc:  # noqa: BLE001 — one client must not stop the run
            d.repo.log(FLOW, c["client_code"], "failed", str(exc), {"flow_arg": c["client_code"]})
    d.repo.log(FLOW, None, "ok", payload={"changed": len(changed), "accounts": accounts, "deals_paid": deals_paid})
    return {"changed": len(changed), "accounts": accounts, "deals_paid": deals_paid}


def mark_paid(d: Deps, account_id: str) -> int:
    n = 0
    for deal in d.zoho.unpaid_closed_deals(account_id):
        d.zoho.update_deal(deal["id"], {"INT_Paid": True})
        for task_id in d.repo.tasks_of("deal_task", deal["id"]):
            task = d.clickup.get_task(task_id)
            fid = field_id(task, PAID_FIELD)
            if fid:
                d.clickup.set_field(task_id, fid, True)
        n += 1
    return n
