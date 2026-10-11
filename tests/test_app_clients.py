"""Webhook endpoints, signatures, retry policy, write guards, Balance mapping."""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import unittest
from decimal import Decimal

import requests

from integration import balance_schema as bs
from integration.app import create_app
from integration.clients import balance as balance_mod
from integration.clients.clickup import verify_signature
from integration.clients.http import ApiError, HttpClient
from integration.clients.zoho import ZohoClient
from tests.fakes import make_deps


class SyncExecutor:
    """Runs submitted work inline so tests see the result immediately."""

    def submit(self, fn, *a, **kw):
        fn(*a, **kw)


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestApp(unittest.TestCase):
    def setUp(self):
        self.d = make_deps()
        self.d.repo.templates = [dict(template_key="contract_prep", trigger_stage="Contracting", clickup_list="L1",
                                      name_pattern="{deal_name}", assignee_ids=[], due_days=1)]
        self.d.zoho.deals["D1"] = {"id": "D1", "Deal_Name": "Deal"}
        self.c = create_app(self.d, executor=SyncExecutor(), start_scheduler=False).test_client()

    def post_zoho(self, payload, token="tok"):
        return self.c.post("/zoho/deal-stage", json=payload, headers={"X-Int-Token": token})

    def test_zoho_requires_token(self):
        self.assertEqual(self.post_zoho({"deal_id": "D1", "stage": "Contracting"}, token="bad").status_code, 401)

    def test_zoho_accept_then_dedupe(self):
        r1 = self.post_zoho({"deal_id": "D1", "stage": "Contracting"})
        r2 = self.post_zoho({"deal_id": "D1", "stage": "Contracting"})
        self.assertEqual((r1.status_code, r1.json["status"]), (202, "accepted"))
        self.assertEqual(r2.json["status"], "skipped")
        self.assertEqual(len(self.d.clickup.created), 1)

    def test_zoho_other_stage_ignored(self):
        self.assertEqual(self.post_zoho({"deal_id": "D1", "stage": "Negotiation"}).json["status"], "ignored")

    def test_zoho_bad_payload(self):
        self.assertEqual(self.post_zoho({"deal_id": "D1"}).status_code, 400)

    def clickup(self, path, payload, secret):
        body = json.dumps(payload).encode()
        return self.c.post(path, data=body, content_type="application/json",
                           headers={"X-Signature": sign(body, secret)})

    def test_clickup_signature(self):
        body = b'{"task_id":"x"}'
        r = self.c.post("/clickup/webhook", data=body, content_type="application/json",
                        headers={"X-Signature": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_clickup_own_change_ignored(self):
        r = self.clickup("/clickup/webhook", {"event": "taskStatusUpdated", "task_id": "t1",
                                              "history_items": [{"id": "h1", "user": {"id": 999}}]}, "sec")
        self.assertEqual(r.json["status"], "own-change")

    def test_clickup_human_change_runs_f4(self):
        self.post_zoho({"deal_id": "D1", "stage": "Contracting"})
        tid = self.d.repo.tasks_of("deal_task", "D1")[0]
        self.d.clickup.tasks[tid]["status"] = {"status": "დახურული", "type": "closed"}
        r = self.clickup("/clickup/webhook", {"event": "taskStatusUpdated", "task_id": tid,
                                              "history_items": [{"id": "h2", "user": {"id": 5}}]}, "sec")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.d.zoho.deals["D1"]["INT_Execution_Status"], "შესრულებულია")

    def test_contracts_endpoint_uses_its_own_secret(self):
        r = self.clickup("/clickup/contracts", {"task_id": "C1", "history_items": []}, "sec")
        self.assertEqual(r.status_code, 401)

    def test_admin_requires_token(self):
        self.assertEqual(self.c.get("/admin/log").status_code, 401)
        self.assertEqual(self.c.get("/admin/log", headers={"X-Int-Token": "tok"}).status_code, 200)

    def test_health(self):
        self.assertEqual(self.c.get("/health").status_code, 200)


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses, **kw):
    c = HttpClient("t", "https://x", sleep=lambda s: None, **kw)
    c.session = FakeSession(responses)
    return c


class TestHttp(unittest.TestCase):
    def test_retry_5xx_on_get(self):
        c = client([FakeResp(503), FakeResp(502), FakeResp(200, {"ok": 1})])
        self.assertEqual(c.get("/a"), {"ok": 1})
        self.assertEqual(len(c.session.calls), 3)

    def test_no_retry_on_400(self):
        c = client([FakeResp(422, {"e": 1})])
        with self.assertRaises(ApiError) as cm:
            c.get("/a")
        self.assertEqual(cm.exception.status, 422)

    def test_post_not_retried_on_5xx(self):
        c = client([FakeResp(500)])
        with self.assertRaises(ApiError):
            c.post("/create")
        self.assertEqual(len(c.session.calls), 1)

    def test_post_retried_on_429(self):
        c = client([FakeResp(429), FakeResp(200, {"id": "1"})])
        self.assertEqual(c.post("/create"), {"id": "1"})

    def test_post_timeout_not_retried(self):
        c = client([requests.Timeout()])
        with self.assertRaises(ApiError):
            c.post("/create")

    def test_gives_up_after_5_retries(self):
        c = client([FakeResp(503)] * 6)
        with self.assertRaises(ApiError):
            c.get("/a")
        self.assertEqual(len(c.session.calls), 6)

    def test_401_refreshes_once(self):
        refreshed = []
        c = client([FakeResp(401), FakeResp(200, {"ok": 1})], on_unauthorized=lambda: refreshed.append(1))
        self.assertEqual(c.get("/a"), {"ok": 1})
        self.assertEqual(refreshed, [1])


class TestGuards(unittest.TestCase):
    def test_zoho_rejects_non_int_fields(self):
        z = ZohoClient(http=client([]), auth=object())
        with self.assertRaises(ValueError):
            z.update_deal("D1", {"Stage": "Closed Deal"})
        with self.assertRaises(ValueError):
            z.update_account("A1", {"Account_Name": "x"})

    def test_balance_client_is_get_only(self):
        src = pathlib.Path(balance_mod.__file__).read_text(encoding="utf-8")
        for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"', ".post(", ".put(", ".patch(", ".delete("):
            self.assertNotIn(verb, src)

    def test_signature(self):
        body = b'{"a":1}'
        self.assertTrue(verify_signature(body, sign(body, "s"), "s"))
        self.assertFalse(verify_signature(body, sign(body, "s"), ""))


class TestBalanceSchema(unittest.TestCase):
    def test_item_reference_objects(self):
        r = bs.item({"Ref": {"UUID": "u1", "Name": "x"}, "Article": "A-9", "Description": "Bolt"})
        self.assertEqual((r["balance_key"], r["code"], r["name"], r["active"]), ("u1", "A-9", "Bolt", True))

    def test_decimal_comma(self):
        self.assertEqual(bs.price({"ItemUUID": "u", "Price": "1 234,50"})["price"], Decimal("1234.50"))

    def test_rows_wrapper(self):
        self.assertEqual(balance_mod._rows({"data": [1]}), [1])
        self.assertEqual(balance_mod._rows([2]), [2])
        self.assertEqual(balance_mod._rows("x"), [])

    def test_sale_key_fallback(self):
        r = bs.sale({"Document": "N5", "ItemUUID": "k", "Date": "2026-01-01T00:00:00"})
        self.assertEqual(r["row_key"], "N5|k|2026-01-01|")


if __name__ == "__main__":
    unittest.main()
