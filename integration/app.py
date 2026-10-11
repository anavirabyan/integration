"""Flask application: inbound webhooks, health check, admin endpoints.

Every webhook is (1) authenticated, (2) stored in ops.inbound_event — which
also deduplicates it — and (3) answered immediately; the flow runs in a
background thread. If the process dies mid-flow, process_inbound picks the
event up again.
"""
from __future__ import annotations

import hmac
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, request

from . import deps as deps_mod
from .clients.clickup import verify_signature
from .flows import ops_jobs

log = logging.getLogger(__name__)


class JsonFormatter(logging.Formatter):
    """One JSON object per line on stdout (Render Logs)."""

    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str) -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def create_app(d: deps_mod.Deps | None = None, executor=None, start_scheduler: bool | None = None) -> Flask:
    app = Flask(__name__)
    get_deps = (lambda: d) if d is not None else deps_mod.get
    pool = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="flow")

    def s():
        return get_deps().settings

    def require_token() -> None:
        token = request.headers.get("X-Int-Token", "")
        if not s().inbound_token or not hmac.compare_digest(token, s().inbound_token):
            abort(401)

    def enqueue(source: str, key: str, flow: str, payload: dict):
        dd = get_deps()
        if not dd.settings.flow_on(flow):
            return jsonify(status="disabled"), 202
        if not dd.repo.insert_event(source, key, flow, payload):
            return jsonify(status="skipped"), 202
        pool.submit(ops_jobs.handle_event, dd, source, key, flow, payload)
        return jsonify(status="accepted"), 202

    # ---------------- Zoho -> F3 ----------------
    @app.post("/zoho/deal-stage")
    def zoho_deal_stage():
        require_token()
        p = request.get_json(silent=True) or {}
        deal_id, stage = str(p.get("deal_id") or ""), str(p.get("stage") or "")
        if not deal_id or not stage:
            abort(400)
        if stage not in {s().stage_contracting, s().stage_closed}:
            return jsonify(status="ignored"), 202
        return enqueue("zoho", f"{deal_id}:{stage}", "F3", {"deal_id": deal_id, "stage": stage})

    # ---------------- ClickUp -> F4 ----------------
    @app.post("/clickup/webhook")
    def clickup_webhook():
        if not verify_signature(request.get_data(), request.headers.get("X-Signature", ""),
                                s().clickup_webhook_secret):
            abort(401)
        return _clickup_event("clickup", "F4")

    # ---------------- ClickUp contracts list -> F7 ----------------
    @app.post("/clickup/contracts")
    def clickup_contracts():
        if not verify_signature(request.get_data(), request.headers.get("X-Signature", ""),
                                s().clickup_contracts_webhook_secret):
            abort(401)
        return _clickup_event("clickup_contracts", "F7")

    def _clickup_event(source: str, flow: str):
        p = request.get_json(silent=True) or {}
        task_id = p.get("task_id")
        if not task_id:
            return jsonify(status="ignored"), 200
        items = p.get("history_items") or []
        # Changes made by the integration's own service user do not trigger flows (loop guard).
        human = [h for h in items if str((h.get("user") or {}).get("id")) != str(s().clickup_service_user_id)]
        if items and not human:
            return jsonify(status="own-change"), 200
        key = str(human[0]["id"]) if human else f"{p.get('event')}:{task_id}:{p.get('webhook_id')}"
        resp, _ = enqueue(source, key, flow, {"task_id": str(task_id), "event": p.get("event")})
        return resp, 200  # ClickUp expects 2xx quickly; anything else is retried by ClickUp

    # ---------------- service ----------------
    @app.get("/health")
    def health():
        from . import db

        ok = db.ping() if d is None else True
        return jsonify(status="ok" if ok else "degraded", db=ok), (200 if ok else 503)

    @app.post("/admin/run/<job>")
    def admin_run(job: str):
        require_token()
        from .scheduler import JOBS

        if job not in JOBS:
            abort(404)
        pool.submit(JOBS[job].fn, get_deps())
        return jsonify(started=job), 202

    @app.get("/admin/log")
    def admin_log():
        require_token()
        rows = get_deps().repo.recent_log(int(request.args.get("limit", 100)), request.args.get("flow"))
        return app.response_class(json.dumps(rows, ensure_ascii=False, default=str),
                                  mimetype="application/json")

    run_sched = s().run_scheduler if start_scheduler is None else start_scheduler
    if run_sched:
        from .scheduler import start

        start(get_deps)
    return app
