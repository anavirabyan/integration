"""Minimal SQL migration runner.

Applies db/migrations/NNN_*.sql in order, each in its own transaction,
and records them in public.schema_migrations. Every schema change must be
a new numbered file; applied files are never edited.

Usage:  python -m integration.migrate
"""
from __future__ import annotations

import logging
import pathlib
import sys

from . import db

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
log = logging.getLogger(__name__)


def pending(applied: set[str]) -> list[pathlib.Path]:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    return [f for f in files if f.name not in applied]


def run() -> list[str]:
    with db.tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    with db.advisory_lock("migrations") as got:
        if not got:
            log.warning("another instance is migrating; skipping")
            return []
        applied = {r["name"] for r in db.fetch_all("SELECT name FROM public.schema_migrations")}
        done = []
        for f in pending(applied):
            with db.tx() as c:
                c.execute(f.read_text(encoding="utf-8"))
                c.execute("INSERT INTO public.schema_migrations (name) VALUES (%s)", (f.name,))
            log.info("applied %s", f.name)
            done.append(f.name)
        return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    names = run()
    print("applied:", ", ".join(names) if names else "nothing (up to date)")
    sys.exit(0)
