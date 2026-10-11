"""Runtime configuration.

All values come from environment variables (Render -> Environment).
Secrets are never hard-coded and never written to logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # ---- Database ----
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))

    # ---- Zoho CRM ----
    zoho_dc: str = field(default_factory=lambda: _env("ZOHO_DC", "eu"))
    zoho_api_version: str = field(default_factory=lambda: _env("ZOHO_API_VERSION", "v8"))
    zoho_client_id: str = field(default_factory=lambda: _env("ZOHO_CLIENT_ID"))
    zoho_client_secret: str = field(default_factory=lambda: _env("ZOHO_CLIENT_SECRET"))
    zoho_refresh_token: str = field(default_factory=lambda: _env("ZOHO_REFRESH_TOKEN"))
    zoho_org_url: str = field(default_factory=lambda: _env("ZOHO_ORG_URL", "https://crm.zoho.eu/crm/org0"))
    # Deal stages that create ClickUp tasks (exact picklist values in Zoho).
    stage_contracting: str = field(default_factory=lambda: _env("ZOHO_STAGE_CONTRACTING", "Contracting"))
    stage_closed: str = field(default_factory=lambda: _env("ZOHO_STAGE_CLOSED", "Closed Deal"))
    # API name of the WIN code field on Deals (owned by Gegidze config, read only).
    zoho_field_win: str = field(default_factory=lambda: _env("ZOHO_FIELD_WIN", "WIN_Code"))
    zoho_field_ident: str = field(default_factory=lambda: _env("ZOHO_FIELD_IDENT", "Identification_Code"))

    # ---- ClickUp ----
    clickup_token: str = field(default_factory=lambda: _env("CLICKUP_TOKEN"))
    clickup_team_id: str = field(default_factory=lambda: _env("CLICKUP_TEAM_ID"))
    clickup_service_user_id: str = field(default_factory=lambda: _env("CLICKUP_SERVICE_USER_ID"))
    # Webhook for the "deal execution" space (F4).
    clickup_webhook_secret: str = field(default_factory=lambda: _env("CLICKUP_WEBHOOK_SECRET"))
    clickup_execution_space_id: str = field(default_factory=lambda: _env("CLICKUP_EXECUTION_SPACE_ID"))
    # Webhook for the supplier contracts list (F7).
    clickup_contracts_webhook_secret: str = field(default_factory=lambda: _env("CLICKUP_CONTRACTS_WEBHOOK_SECRET"))
    clickup_contracts_list_id: str = field(default_factory=lambda: _env("CLICKUP_CONTRACTS_LIST_ID"))
    clickup_incidents_list_id: str = field(default_factory=lambda: _env("CLICKUP_INCIDENTS_LIST_ID"))

    # ---- Balance (1C) — read only ----
    balance_base_url: str = field(default_factory=lambda: _env("BALANCE_BASE_URL"))
    balance_user: str = field(default_factory=lambda: _env("BALANCE_USER"))
    balance_pass: str = field(default_factory=lambda: _env("BALANCE_PASS"))
    balance_node_code: str = field(default_factory=lambda: _env("BALANCE_NODE_CODE"))
    # Response header that carries the last change number (to be confirmed on the real API).
    balance_change_header: str = field(default_factory=lambda: _env("BALANCE_CHANGE_HEADER", "LastChangeNumber"))
    # Which price type goes to Zoho Unit_Price, which warehouses are summed into INT_Stock.
    balance_price_type: str = field(default_factory=lambda: _env("BALANCE_PRICE_TYPE"))
    balance_warehouses: list[str] = field(default_factory=lambda: _list("BALANCE_WAREHOUSES"))

    # ---- Middleware ----
    inbound_token: str = field(default_factory=lambda: _env("INBOUND_TOKEN"))
    timezone: str = field(default_factory=lambda: _env("TZ_NAME", "Asia/Tbilisi"))
    run_scheduler: bool = field(default_factory=lambda: _bool("RUN_SCHEDULER", True))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    # Per-flow kill switches (rollback plan: disable one flow without redeploying code).
    flows_enabled: dict[str, bool] = field(default_factory=lambda: {
        f: _bool(f"FLOW_{f}_ENABLED", True) for f in ("F1", "F2", "F3", "F4", "F5", "F7")
    })

    @property
    def zoho_api_base(self) -> str:
        return f"https://www.zohoapis.{self.zoho_dc}/crm/{self.zoho_api_version}"

    @property
    def zoho_accounts_url(self) -> str:
        return f"https://accounts.zoho.{self.zoho_dc}/oauth/v2/token"

    def flow_on(self, flow: str) -> bool:
        return self.flows_enabled.get(flow, True)


settings = Settings()
