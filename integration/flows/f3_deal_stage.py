"""F3 — Zoho deal stage -> ClickUp execution tasks. Event-driven (Deluge -> /zoho/deal-stage).

For every active template whose trigger_stage equals the new stage:
  * skip if ops.id_map already has (deal_task, deal_id, template_key)  -> idempotent
  * create the ClickUp task, record it in ops.id_map
Then write INT_ClickUp_Task_IDs and INT_Execution_Status on the deal.

The task is recorded in id_map right after it is created, so a crash between
templates never produces a duplicate on retry.
"""
from __future__ import annotations

from datetime import timedelta

from ..clients.clickup import field_id, ms
from ..clients.zoho import deal_url
from ..deps import Deps

FLOW = "F3"
STATUS_NOT_STARTED = "არ დაწყებულა"


def task_body(tpl: dict, deal: dict, d: Deps) -> dict:
    name = tpl["name_pattern"].format(deal_name=deal.get("Deal_Name") or deal["id"])
    account = (deal.get("Account_Name") or {}).get("name", "")
    lines = [
        f"Zoho: {deal_url(deal['id'])}",
        f"კლიენტი: {account}" if account else "",
        f"თანხა: {deal.get('Amount')}" if deal.get("Amount") is not None else "",
        f"WIN: {deal.get(d.settings.zoho_field_win)}" if deal.get(d.settings.zoho_field_win) else "",
    ]
    body = {
        "name": name[:250],
        "description": "\n".join(x for x in lines if x),
        "assignees": [int(a) for a in tpl.get("assignee_ids") or [] if str(a).isdigit()],
    }
    if tpl.get("due_days") is not None:
        body["due_date"] = ms(d.today() + timedelta(days=int(tpl["due_days"])))
    return body


def run(d: Deps, deal_id: str, stage: str) -> dict:
    templates = d.repo.templates_for_stage(stage)
    if not templates:
        d.repo.log(FLOW, deal_id, "skipped", f"no templates for stage {stage}")
        return {"created": []}

    deal = d.zoho.get_deal(deal_id)
    created = []
    for tpl in templates:
        if d.repo.id_exists("deal_task", deal_id, tpl["template_key"]):
            continue
        task = d.clickup.create_task(tpl["clickup_list"], task_body(tpl, deal, d))
        d.repo.id_add("deal_task", deal_id, task["id"], tpl["template_key"])
        _link_fields(d, task, deal_id, tpl["template_key"])
        created.append(task["id"])

    if created:
        d.zoho.update_deal(deal_id, {
            "INT_ClickUp_Task_IDs": ",".join(d.repo.tasks_of("deal_task", deal_id)),
            "INT_Execution_Status": STATUS_NOT_STARTED,
        })
    d.repo.log(FLOW, deal_id, "ok", payload={"stage": stage, "created": created})
    return {"created": created}


def _link_fields(d: Deps, task: dict, deal_id: str, template_key: str) -> None:
    """Fill the link custom fields by name (ids differ per list, so look them up on the task)."""
    values = {"Zoho Deal ID": deal_id, "Zoho Deal URL": deal_url(deal_id), "Template": template_key}
    for name, value in values.items():
        fid = field_id(task, name)
        if fid:
            d.clickup.set_field(task["id"], fid, value)
