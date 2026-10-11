"""F7 — supplier contract payment schedule (5% / 15% / 80%) in ClickUp.

Source: process described by procurement. Balance (1C) cannot remind by a schedule,
so every supplier contract is a task in the ClickUp list "კონტრაქტების რეგისტრაცია"
and the middleware keeps three payment subtasks on it:

  step    share  anchor                    internal due                   reminder
  pay_05   5%    contract date             contract + 7 days              2 days before
  pay_15  15%    port handover date        handover date                  3 days before
  pay_80  80%    15% payment date          15% paid + (limit - 10) days   10 days before
                                           (limit = contract term, default 70 -> due on day 60)

The 80% payment is the critical one: the internal deadline sits 10 days before the
contract deadline, and the reminder another 10 days earlier, leaving time to prepare,
approve and transfer. When the 15% subtask is closed, its close date is written to
the contract ("15%-ის გადახდის თარიღი") and the 80% subtask is rescheduled.

Contract task custom fields (matched by NAME, so no field ids in code) — see F_* below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..clients.clickup import field_id, field_value, from_ms, is_closed, ms
from ..deps import Deps

FLOW = "F7"

F_SUPPLIER = "მწარმოებელი"
F_NUMBER = "კონტრაქტის ნომერი"
F_AMOUNT = "კონტრაქტის თანხა"
F_CURRENCY = "ვალუტა"
F_CONTRACT_DATE = "კონტრაქტის თარიღი"
F_PORT_DATE = "პორტში მიწოდების თარიღი"
F_PAID15_DATE = "15%-ის გადახდის თარიღი"
F_TERM_80 = "80%-ის ვადა კონტრაქტით, დღე"

DEFAULT_TERM_80 = 70
INTERNAL_BUFFER_80 = 10


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    share: Decimal
    anchor: str          # contract field the due date is counted from
    due_days: int | None  # None -> computed from the contract term (80%)
    remind_days: int


SCHEDULE = [
    Step("pay_05", "5%", Decimal("0.05"), "contract_date", 7, 2),
    Step("pay_15", "15%", Decimal("0.15"), "port_date", 0, 3),
    Step("pay_80", "80%", Decimal("0.80"), "paid15_date", None, 10),
]
STEP_BY_KEY = {s.key: s for s in SCHEDULE}


# ---------------------------------------------------------------- pure logic
def parse_contract(task: dict) -> dict:
    amount_raw = field_value(task, F_AMOUNT)
    currency = field_value(task, F_CURRENCY) or _money_currency(task) or "USD"
    term = field_value(task, F_TERM_80)
    return {
        "id": task["id"],
        "number": str(field_value(task, F_NUMBER) or task.get("name") or task["id"]),
        "supplier": field_value(task, F_SUPPLIER),
        "amount": Decimal(str(amount_raw)) if amount_raw not in (None, "") else None,
        "currency": str(currency),
        "contract_date": from_ms(field_value(task, F_CONTRACT_DATE)),
        "port_date": from_ms(field_value(task, F_PORT_DATE)),
        "paid15_date": from_ms(field_value(task, F_PAID15_DATE)),
        "term_80": int(Decimal(str(term))) if term not in (None, "") else DEFAULT_TERM_80,
    }


def plan_payments(c: dict) -> list[dict]:
    """Amounts and dates for the three steps. Dates are None until their anchor is known."""
    plan = []
    for s in SCHEDULE:
        due_days = s.due_days if s.due_days is not None else max(c["term_80"] - INTERNAL_BUFFER_80, 0)
        base: date | None = c.get(s.anchor)
        due = base + timedelta(days=due_days) if base else None
        amount = (c["amount"] * s.share).quantize(Decimal("0.01"), ROUND_HALF_UP) if c["amount"] is not None else None
        plan.append({
            "key": s.key,
            "label": s.label,
            "amount": amount,
            "due": due,
            "remind": due - timedelta(days=s.remind_days) if due else None,
        })
    return plan


def payment_name(p: dict, c: dict) -> str:
    amount = f"{p['amount']:,.2f} {c['currency']}" if p["amount"] is not None else "თანხა უცნობია"
    return f"{p['label']} გადახდა — №{c['number']} — {amount}"


def subtask_body(p: dict, c: dict) -> dict:
    body: dict[str, Any] = {"name": payment_name(p, c)}
    if p["due"]:
        body["due_date"] = ms(p["due"])
        body["start_date"] = ms(p["remind"])
        body["priority"] = 1 if p["key"] == "pay_80" else 3  # 1 = urgent, 3 = normal
    return body


# ---------------------------------------------------------------- IO
def sync_contract(d: Deps, contract_id: str) -> dict:
    """Create / update the three payment subtasks of one contract and copy them to dwh."""
    task = d.clickup.get_task(contract_id)
    if task.get("parent"):
        # A subtask that is not a payment (or a payment whose map is missing): nothing to do.
        return {"skipped": "not a contract"}
    c = parse_contract(task)
    subtasks = {s["id"]: s for s in task.get("subtasks") or []}
    created, updated = [], []

    for p in plan_payments(c):
        body = subtask_body(p, c)
        sub_id = d.repo.id_get("payment", contract_id, p["key"])
        if sub_id:
            current = subtasks.get(sub_id) or {}
            if _differs(current, body):
                d.clickup.update_task(sub_id, body)
                updated.append(sub_id)
        else:
            new = d.clickup.create_task(d.settings.clickup_contracts_list_id, {**body, "parent": contract_id})
            sub_id = new["id"]
            d.repo.id_add("payment", contract_id, sub_id, p["key"])
            created.append(sub_id)
            current = new
        sub = subtasks.get(sub_id) or current
        d.repo.upsert_supplier_payment({
            "clickup_id": sub_id, "contract_id": contract_id, "contract_no": c["number"],
            "supplier": c["supplier"], "step": p["key"], "amount": p["amount"], "currency": c["currency"],
            "due_date": p["due"], "remind_date": p["remind"], "paid": is_closed(sub),
            "paid_date": from_ms(sub.get("date_closed")) if is_closed(sub) else None,
        })
    d.repo.log(FLOW, contract_id, "ok", payload={"created": created, "updated": updated})
    return {"created": created, "updated": updated}


def on_task_event(d: Deps, task_id: str) -> dict:
    """Entry point for the contracts-list webhook."""
    owner = d.repo.owner_of("payment", task_id)
    if not owner:
        return sync_contract(d, task_id)  # a contract task itself

    contract_id, step = owner
    if step == "pay_15":
        sub = d.clickup.get_task(task_id)
        if is_closed(sub):
            contract = d.clickup.get_task(contract_id)
            if not field_value(contract, F_PAID15_DATE):
                fid = field_id(contract, F_PAID15_DATE)
                paid_on = from_ms(sub.get("date_closed")) or d.today()
                if fid:
                    d.clickup.set_field(contract_id, fid, ms(paid_on))
    return sync_contract(d, contract_id)


def send_reminders(d: Deps) -> int:
    """Daily: comment on payments whose reminder day has come; flag overdue ones."""
    n = 0
    for p in d.repo.payments_to_remind(d.today()):
        step = STEP_BY_KEY.get(p["step"])
        d.clickup.comment(
            p["clickup_id"],
            f"⏰ {step.label if step else p['step']} გადახდა — №{p['contract_no']} — "
            f"{p['amount']} {p['currency']}. ვადა: {p['due_date']}. "
            "მოამზადეთ განაცხადი, დაამტკიცეთ და გადარიცხეთ.",
            notify_all=True,
        )
        n += 1
    for p in d.repo.overdue_payments(d.today()):
        d.clickup.comment(p["clickup_id"], f"⚠️ ვადა გადაცილებულია ({p['due_date']}).", notify_all=True)
    d.repo.log(FLOW, None, "ok", payload={"reminders": n})
    return n


def _money_currency(task: dict) -> str | None:
    for f in task.get("custom_fields") or []:
        if f.get("name", "").strip() == F_AMOUNT:
            return (f.get("type_config") or {}).get("currency_type")
    return None


def _differs(current: dict, body: dict) -> bool:
    if not current:
        return True
    if current.get("name") != body["name"]:
        return True
    for k in ("due_date", "start_date"):
        if k in body and str(current.get(k) or "") != str(body[k]):
            return True
    return False
