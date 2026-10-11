"""F4 — ClickUp task status -> Zoho deal INT_Execution_Status. Event-driven (ClickUp webhook).

The status is recomputed from ALL tasks of the deal every time (not incremented),
so replaying or reordering events always converges to the right value.

Loop protection: the middleware writes only INT_* fields and never Stage;
the Zoho workflow that triggers F3 fires only on Stage changes, so F4 can
never re-trigger F3.
"""
from __future__ import annotations

from ..clients.clickup import is_closed
from ..deps import Deps

FLOW = "F4"
NOT_STARTED = "არ დაწყებულა"
IN_PROGRESS = "მიმდინარე"
DONE = "შესრულებულია"


def aggregate(tasks: list[dict]) -> str:
    """All closed -> done; at least one moved off its first status -> in progress."""
    if not tasks:
        return NOT_STARTED
    if all(is_closed(t) for t in tasks):
        return DONE
    if any(is_closed(t) or (t.get("status") or {}).get("type") not in (None, "open") for t in tasks):
        return IN_PROGRESS
    return NOT_STARTED


def run(d: Deps, task_id: str) -> dict:
    owner = d.repo.owner_of("deal_task", task_id)
    if not owner:
        d.repo.log(FLOW, task_id, "skipped", "task is not linked to a Zoho deal")
        return {"status": None}
    deal_id, _ = owner

    tasks = []
    for tid in d.repo.tasks_of("deal_task", deal_id):
        try:
            tasks.append(d.clickup.get_task(tid))
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:  # task was deleted in ClickUp: ignore it
                continue
            raise
    status = aggregate(tasks)
    d.zoho.update_deal(deal_id, {"INT_Execution_Status": status})
    d.repo.log(FLOW, deal_id, "ok", payload={"task": task_id, "status": status})
    return {"deal_id": deal_id, "status": status}
