"""Register the two ClickUp webhooks once and print their secrets.

    python -m scripts.register_clickup_webhooks https://<service>.onrender.com

Put the printed secrets into Render -> Environment:
    CLICKUP_WEBHOOK_SECRET            (deal execution space, F4)
    CLICKUP_CONTRACTS_WEBHOOK_SECRET  (supplier contracts list, F7)
"""
from __future__ import annotations

import sys

from integration.clients.clickup import ClickUpClient
from integration.config import settings


def main(base_url: str) -> None:
    cu = ClickUpClient()
    base_url = base_url.rstrip("/")

    if settings.clickup_execution_space_id:
        r = cu.register_webhook(f"{base_url}/clickup/webhook", ["taskStatusUpdated", "taskDeleted"],
                                space_id=settings.clickup_execution_space_id)
        print("CLICKUP_WEBHOOK_SECRET =", r["webhook"]["secret"])
    else:
        print("skip F4 webhook: CLICKUP_EXECUTION_SPACE_ID is empty")

    if settings.clickup_contracts_list_id:
        r = cu.register_webhook(f"{base_url}/clickup/contracts",
                                ["taskCreated", "taskUpdated", "taskStatusUpdated"],
                                list_id=settings.clickup_contracts_list_id)
        print("CLICKUP_CONTRACTS_WEBHOOK_SECRET =", r["webhook"]["secret"])
    else:
        print("skip F7 webhook: CLICKUP_CONTRACTS_LIST_ID is empty")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
