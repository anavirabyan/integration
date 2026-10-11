"""ClickUp API v2 client (service-user personal token)."""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..config import settings
from .http import HttpClient

API = "https://api.clickup.com/api/v2"


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """ClickUp signs the raw body: X-Signature = hex(HMAC-SHA256(secret, body))."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def ms(d: date | datetime) -> int:
    """Date -> ClickUp epoch milliseconds (local midnight for plain dates)."""
    if not isinstance(d, datetime):
        d = datetime.combine(d, time(9, 0), tzinfo=ZoneInfo(settings.timezone))
    return int(d.timestamp() * 1000)


def from_ms(value: Any) -> date | None:
    if value in (None, "", 0, "0"):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=ZoneInfo(settings.timezone)).date()


def from_ms_dt(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def field_value(task: dict, name: str) -> Any:
    """Custom field value by its (Georgian) name — no field ids in code."""
    for f in task.get("custom_fields") or []:
        if f.get("name", "").strip() == name:
            value = f.get("value")
            # Dropdowns return the option index; translate to the option name.
            if f.get("type") == "drop_down" and value is not None:
                for opt in (f.get("type_config") or {}).get("options") or []:
                    if opt.get("orderindex") == value or opt.get("id") == value:
                        return opt.get("name")
            return value
    return None


def field_id(task: dict, name: str) -> str | None:
    for f in task.get("custom_fields") or []:
        if f.get("name", "").strip() == name:
            return f.get("id")
    return None


def is_closed(task: dict) -> bool:
    return (task.get("status") or {}).get("type") in {"closed", "done"}


class ClickUpClient:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient(
            "clickup", API, timeout=30,
            auth_header=lambda: {"Authorization": settings.clickup_token},
        )

    def get_task(self, task_id: str) -> dict:
        return self.http.get(f"/task/{task_id}", params={"include_subtasks": "true"})

    def create_task(self, list_id: str, body: dict) -> dict:
        # Not idempotent: retried only on 429 (see http.py). Duplicates are
        # additionally prevented by ops.id_map before this call.
        return self.http.post(f"/list/{list_id}/task", json=body)

    def update_task(self, task_id: str, body: dict) -> dict:
        return self.http.put(f"/task/{task_id}", json=body)

    def set_field(self, task_id: str, fid: str, value: Any) -> None:
        self.http.request("POST", f"/task/{task_id}/field/{fid}", idempotent=True, json={"value": value})

    def comment(self, task_id: str, text: str, notify_all: bool = False) -> None:
        self.http.post(f"/task/{task_id}/comment", json={"comment_text": text, "notify_all": notify_all})

    def tasks_updated_since(self, since_ms: int, space_ids: list[str] | None = None) -> list[dict]:
        """Filtered team tasks (paged, 100 per page), including closed and subtasks."""
        out, page = [], 0
        while True:
            params: list[tuple[str, Any]] = [
                ("page", page), ("date_updated_gt", since_ms), ("include_closed", "true"),
                ("subtasks", "true"), ("order_by", "updated"), ("reverse", "true"),
            ]
            for s in space_ids or []:
                params.append(("space_ids[]", s))
            data = self.http.get(f"/team/{settings.clickup_team_id}/task", params=params)
            tasks = data.get("tasks") or []
            out.extend(tasks)
            if data.get("last_page", True) or not tasks:
                return out
            page += 1

    def register_webhook(self, endpoint: str, events: list[str], *,
                         space_id: str | None = None, list_id: str | None = None) -> dict:
        body: dict[str, Any] = {"endpoint": endpoint, "events": events}
        if space_id:
            body["space_id"] = space_id
        if list_id:
            body["list_id"] = list_id
        return self.http.post(f"/team/{settings.clickup_team_id}/webhook", json=body)
