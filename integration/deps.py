"""Dependency container passed to every flow.

Production builds it with real clients; tests build it with fakes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings, settings as default_settings


@dataclass
class Deps:
    repo: Any
    zoho: Any
    clickup: Any
    balance: Any
    settings: Settings = default_settings
    now: Callable[[], datetime] = field(default=lambda: datetime.now(ZoneInfo(default_settings.timezone)))

    def today(self) -> date:
        return self.now().date()


_deps: Deps | None = None


def get() -> Deps:
    """Real dependencies, built once per process."""
    global _deps
    if _deps is None:
        from .clients.balance import BalanceClient
        from .clients.clickup import ClickUpClient
        from .clients.zoho import ZohoClient
        from .repo import Repo

        _deps = Deps(repo=Repo(), zoho=ZohoClient(), clickup=ClickUpClient(), balance=BalanceClient())
    return _deps


def set_for_tests(d: Deps | None) -> None:
    global _deps
    _deps = d
