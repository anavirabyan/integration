"""Service jobs: inbound queue, retries of failed records, alerts, log cleanup."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..deps import Deps
from . import f1_products, f2_balances, f3_deal_stage, f4_task_status, f7_supplier_payments

log = logging.getLogger(__name__)


def dispatch(d: Deps, flow: str, payload: dict) -> dict:
    """Run the flow an inbound event belongs to."""
    if not d.settings.flow_on(flow):
        return {"disabled": flow}
    if flow == "F3":
        return f3_deal_stage.run(d, payload["deal_id"], payload["stage"])
    if flow == "F4":
        return f4_task_status.run(d, payload["task_id"])
    if flow == "F7":
        return f7_supplier_payments.on_task_event(d, payload["task_id"])
    raise ValueError(f"unknown flow {flow}")


def handle_event(d: Deps, source: str, key: str, flow: str, payload: dict) -> None:
    """Process one stored event; on error keep it pending for process_inbound."""
    try:
        dispatch(d, flow, payload)
        d.repo.event_done(source, key)
    except Exception as exc:  # noqa: BLE001
        log.exception("%s event %s failed", flow, key)
        d.repo.event_failed(source, key, str(exc))
        d.repo.log(flow, payload.get("deal_id") or payload.get("task_id"), "retry", str(exc), payload)


def process_inbound(d: Deps) -> int:
    """Every 30 s: re-run events that were received but never finished
    (process restarted, upstream API was down, ...)."""
    events = d.repo.pending_events()
    for e in events:
        handle_event(d, e["source"], e["event_key"], e["flow"], e["payload"])
    return len(events)


def retry_failed(d: Deps) -> int:
    """Every 10 min: retry single records that failed in scheduled flows (F1 products, F2 clients)."""
    n = 0
    for row in d.repo.failed_to_retry():
        d.repo.mark_retried(row["id"])
        ref = row.get("entity_ref")
        if not ref:
            continue
        try:
            if row["flow"] == "F1":
                keys = d.repo.keys_for_codes([ref])
                f1_products.push_products(d, set(keys))
            elif row["flow"] == "F2":
                retry_client(d, ref)
            else:
                continue
            n += 1
        except Exception as exc:  # noqa: BLE001
            d.repo.log(row["flow"], ref, "failed", str(exc), attempts=(row.get("attempts") or 1) + 1)
    return n


def retry_client(d: Deps, client_code: str) -> None:
    debt = d.repo.client_debt(client_code)
    if debt is None:
        return
    acc = d.zoho.find_account_by_ident(client_code)
    if not acc:
        return
    d.zoho.update_account(acc["id"], {"INT_Balance_Debt": float(debt)})
    if debt <= 0:
        f2_balances.mark_paid(d, acc["id"])


def check_alerts(d: Deps) -> bool:
    """>10 failures in the last hour, or F1 without success for 2 h -> incident task in ClickUp."""
    s = d.repo.alert_stats()
    failed = int(s.get("failed_1h") or 0)
    last_ok: datetime | None = s.get("last_f1_ok")
    stale = last_ok is not None and d.now() - last_ok > timedelta(hours=2)
    if not (failed > 10 or stale):
        return False
    # At most one incident task every 2 hours while the problem lasts.
    last = d.repo.get_cursor("alert:last")
    if last and d.now() - datetime.fromisoformat(last) < timedelta(hours=2):
        return False
    d.repo.set_cursor("alert:last", d.now().isoformat())
    if not d.settings.clickup_incidents_list_id:
        log.error("ALERT: failed_1h=%s last_f1_ok=%s (no incidents list configured)", failed, last_ok)
        return True
    d.clickup.create_task(d.settings.clickup_incidents_list_id, {
        "name": f"⚠️ ინტეგრაცია: {failed} შეცდომა ბოლო საათში / F1 ბოლო წარმატება: {last_ok}",
        "description": "იხილეთ GET /admin/log",
        "priority": 1,
    })
    return True


def purge(d: Deps) -> int:
    return d.repo.purge_log(90)
