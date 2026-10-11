"""Background schedule (APScheduler inside the web process).

Every job takes a PostgreSQL advisory lock first; if another instance holds it
(e.g. old and new instance overlap during a Render deploy) the run is skipped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .deps import Deps
from .flows import f1_products, f2_balances, f5_dwh, f7_supplier_payments, ops_jobs

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    fn: Callable[[Deps], object]
    trigger: str
    args: dict
    flow: str | None = None


JOBS: dict[str, Job] = {
    "sync_items_prices_stocks": Job(f1_products.run, "interval", {"minutes": 15}, "F1"),
    "sync_client_balances":     Job(f2_balances.run, "interval", {"minutes": 30}, "F2"),
    "load_zoho_deals":          Job(f5_dwh.load_zoho_deals, "interval", {"minutes": 60}, "F5"),
    "load_clickup_tasks":       Job(f5_dwh.load_clickup_tasks, "interval", {"minutes": 60}, "F5"),
    "load_sales_report":        Job(f5_dwh.load_sales_report, "cron", {"hour": 3, "minute": 0}, "F5"),
    "snapshot_stock":           Job(f5_dwh.snapshot_stock, "cron", {"hour": 3, "minute": 30}, "F5"),
    "supplier_reminders":       Job(f7_supplier_payments.send_reminders, "cron", {"hour": 9, "minute": 5}, "F7"),
    "process_inbound":          Job(ops_jobs.process_inbound, "interval", {"seconds": 30}),
    "retry_failed":             Job(ops_jobs.retry_failed, "interval", {"minutes": 10}),
    "check_alerts":             Job(ops_jobs.check_alerts, "interval", {"minutes": 15}),
    "purge_log":                Job(ops_jobs.purge, "cron", {"hour": 4, "minute": 0}),
}


def guarded(name: str, job: Job, get_deps: Callable[[], Deps]) -> Callable[[], None]:
    def run() -> None:
        from . import db

        d = get_deps()
        if job.flow and not d.settings.flow_on(job.flow):
            return
        with db.advisory_lock(f"job:{name}") as got:
            if not got:
                log.info("skip %s: locked by another instance", name)
                return
            try:
                result = job.fn(d)
                log.info("job %s done: %s", name, result)
            except Exception as exc:  # noqa: BLE001 — a failed run must not kill the scheduler
                log.exception("job %s failed", name)
                d.repo.log(job.flow or "OPS", name, "failed", str(exc))
    return run


def start(get_deps: Callable[[], Deps]):
    from apscheduler.schedulers.background import BackgroundScheduler

    tz = get_deps().settings.timezone
    sched = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 300})
    for name, job in JOBS.items():
        sched.add_job(guarded(name, job, get_deps), job.trigger, id=name, **job.args)
    sched.start()
    log.info("scheduler started with %d jobs (%s)", len(JOBS), tz)
    return sched
