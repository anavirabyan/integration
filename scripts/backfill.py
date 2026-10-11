"""First full load (run once after migrations, and any time a full resync is needed).

    python -m scripts.backfill                    # items, prices, stock, debts, deals, tasks
    python -m scripts.backfill --sales 2024-01-01 # + sales history from that date (reports 4, 6)
    python -m scripts.backfill --contracts        # + F7 for every task in the contracts list

Without LastReadChange Balance returns everything, so clearing the cursors
turns the normal incremental flows into a full load.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from integration import deps
from integration.flows import f1_products, f2_balances, f5_dwh, f7_supplier_payments


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sales", type=date.fromisoformat, help="load SalesReport from this date to yesterday")
    ap.add_argument("--contracts", action="store_true", help="sync payment subtasks for all contracts")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    d = deps.get()
    for s in ("Items", "Prices", "Stocks"):
        d.repo.clear_cursor(f"balance:{s}")
    for s in ("zoho:Deals", "clickup:tasks"):
        d.repo.clear_cursor(s)

    print("F1:", f1_products.run(d))
    print("F2:", f2_balances.run(d))
    print("F5 deals:", f5_dwh.load_zoho_deals(d))
    print("F5 tasks:", f5_dwh.load_clickup_tasks(d))
    print("F5 snapshot:", f5_dwh.snapshot_stock(d))
    if args.sales:
        print("F5 sales:", f5_dwh.load_sales_report(d, args.sales, d.today()))
    if args.contracts:
        lst = d.settings.clickup_contracts_list_id
        page, n = 0, 0
        while True:
            data = d.clickup.http.get(f"/list/{lst}/task", params={"page": page, "include_closed": "true"})
            tasks = [t for t in data.get("tasks") or [] if not t.get("parent")]
            for t in tasks:
                f7_supplier_payments.sync_contract(d, t["id"])
                n += 1
            if data.get("last_page", True) or not data.get("tasks"):
                break
            page += 1
        print("F7 contracts:", n)


if __name__ == "__main__":
    main()
