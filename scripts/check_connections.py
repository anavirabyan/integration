"""Check every external connection with read-only calls and print a checklist.

    python -m scripts.check_connections

Run it in the Render Shell after filling Environment. Nothing is written anywhere.
Exit code 0 = everything OK, 1 = something needs fixing.
"""
from __future__ import annotations

import sys
from typing import Callable

from integration.config import settings

REQUIRED = {
    "DATABASE_URL": settings.database_url,
    "INBOUND_TOKEN": settings.inbound_token,
    "ZOHO_CLIENT_ID": settings.zoho_client_id,
    "ZOHO_CLIENT_SECRET": settings.zoho_client_secret,
    "ZOHO_REFRESH_TOKEN": settings.zoho_refresh_token,
    "CLICKUP_TOKEN": settings.clickup_token,
    "CLICKUP_TEAM_ID": settings.clickup_team_id,
    "CLICKUP_SERVICE_USER_ID": settings.clickup_service_user_id,
    "BALANCE_BASE_URL": settings.balance_base_url,
    "BALANCE_USER": settings.balance_user,
    "BALANCE_PASS": settings.balance_pass,
    "BALANCE_NODE_CODE": settings.balance_node_code,
}
OPTIONAL = {
    "CLICKUP_EXECUTION_SPACE_ID": settings.clickup_execution_space_id,
    "CLICKUP_CONTRACTS_LIST_ID": settings.clickup_contracts_list_id,
    "CLICKUP_INCIDENTS_LIST_ID": settings.clickup_incidents_list_id,
    "CLICKUP_WEBHOOK_SECRET": settings.clickup_webhook_secret,
    "CLICKUP_CONTRACTS_WEBHOOK_SECRET": settings.clickup_contracts_webhook_secret,
    "BALANCE_PRICE_TYPE": settings.balance_price_type,
}

ZOHO_FIELDS = {
    "Deals": ["INT_ClickUp_Task_IDs", "INT_Execution_Status", "INT_Paid"],
    "Products": ["INT_Balance_Code", "INT_Stock", "INT_Last_Sync"],
    "Accounts": ["INT_Balance_Debt", settings.zoho_field_ident],
}
CONTRACT_FIELDS = ["მწარმოებელი", "კონტრაქტის ნომერი", "კონტრაქტის თანხა", "კონტრაქტის თარიღი",
                   "პორტში მიწოდების თარიღი", "15%-ის გადახდის თარიღი", "80%-ის ვადა კონტრაქტით, დღე"]

results: list[tuple[bool, str, str]] = []


def check(name: str, fn: Callable[[], str]) -> None:
    try:
        results.append((True, name, fn() or ""))
    except Exception as exc:  # noqa: BLE001 — report and continue with the next check
        results.append((False, name, str(exc)[:300]))


def env_required() -> str:
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        raise RuntimeError("missing: " + ", ".join(missing))
    empty = [k for k, v in OPTIONAL.items() if not v]
    return "optional still empty: " + ", ".join(empty) if empty else "all set"


def database() -> str:
    from integration import db

    names = [r["name"] for r in db.fetch_all("SELECT name FROM public.schema_migrations ORDER BY name")]
    tpl = db.fetch_one("SELECT count(*) AS n, count(*) FILTER (WHERE clickup_list LIKE '<%%') AS todo "
                       "FROM ops.task_template")
    note = f"; templates: {tpl['n']}" + (f" ({tpl['todo']} still have <LIST_ID>)" if tpl["todo"] else "")
    return "migrations: " + ", ".join(names) + note


def zoho_token() -> str:
    from integration.clients.zoho import ZohoAuth

    ZohoAuth().header()
    return "access token received"


def zoho_fields() -> str:
    from integration.clients.zoho import ZohoClient

    z = ZohoClient()
    missing = []
    for module, fields in ZOHO_FIELDS.items():
        have = {f["api_name"] for f in z.http.get("/settings/fields", params={"module": module}).get("fields", [])}
        missing += [f"{module}.{f}" for f in fields if f not in have]
    if missing:
        raise RuntimeError("fields not created yet: " + ", ".join(missing))
    return "all INT_ fields exist"


def clickup_user() -> str:
    from integration.clients.clickup import ClickUpClient

    u = ClickUpClient().http.get("/user")["user"]
    if settings.clickup_service_user_id and str(u["id"]) != str(settings.clickup_service_user_id):
        raise RuntimeError(f"token belongs to user {u['id']}, CLICKUP_SERVICE_USER_ID is "
                           f"{settings.clickup_service_user_id}")
    return f"token user: {u.get('username')} ({u['id']})"


def clickup_contracts_list() -> str:
    from integration.clients.clickup import ClickUpClient

    if not settings.clickup_contracts_list_id:
        raise RuntimeError("CLICKUP_CONTRACTS_LIST_ID is empty")
    fields = ClickUpClient().http.get(f"/list/{settings.clickup_contracts_list_id}/field").get("fields", [])
    have = {f["name"].strip() for f in fields}
    missing = [f for f in CONTRACT_FIELDS if f not in have]
    if missing:
        raise RuntimeError("custom fields missing: " + ", ".join(missing))
    return "all contract fields exist"


def balance() -> str:
    from integration.clients.balance import BalanceClient

    rows = BalanceClient().full("Warehouses")
    return f"{len(rows)} warehouses readable"


def needs(*keys: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
    """Skip a remote check when its own settings are still empty."""
    def wrap(fn: Callable[[], str]) -> Callable[[], str]:
        def run() -> str:
            missing = [k for k in keys if not REQUIRED.get(k)]
            if missing:
                raise RuntimeError("skipped, set first: " + ", ".join(missing))
            return fn()
        return run
    return wrap


def main() -> int:
    zoho = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    cu = ("CLICKUP_TOKEN",)
    bal = ("BALANCE_BASE_URL", "BALANCE_USER", "BALANCE_PASS")
    check("Environment", env_required)
    check("PostgreSQL", needs("DATABASE_URL")(database))
    check("Zoho token", needs(*zoho)(zoho_token))
    check("Zoho INT_ fields", needs(*zoho)(zoho_fields))
    check("ClickUp token", needs(*cu)(clickup_user))
    check("ClickUp contracts list", needs(*cu)(clickup_contracts_list))
    check("Balance web services", needs(*bal)(balance))
    for ok, name, info in results:
        print(f"{'✅' if ok else '❌'} {name:<24} {info}")
    return 0 if all(ok for ok, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
