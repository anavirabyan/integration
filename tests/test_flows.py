"""Flow tests with in-memory fakes. Run:  python -m unittest  (or pytest)."""
from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from integration.clients.balance import Page
from integration.flows import f1_products, f2_balances, f3_deal_stage, f4_task_status, f5_dwh
from integration.flows import f7_supplier_payments as f7
from integration.flows import ops_jobs
from tests.fakes import make_deps, ms_of


class TestF1(unittest.TestCase):
    def setUp(self):
        self.d = make_deps(balance_price_type="Retail", balance_warehouses=["Main"])
        self.d.balance.pages = {
            "Items": Page([{"UUID": "k1", "Code": "A-1", "Name": "Filter"},
                           {"UUID": "k2", "Code": "A-2", "Name": "Pump", "DeletionMark": True}], "c1"),
            "Prices": Page([{"ItemUUID": "k1", "PriceType": "Retail", "Price": "12,50"},
                            {"ItemUUID": "k1", "PriceType": "Wholesale", "Price": "10"}], "c2"),
            "Stocks": Page([{"ItemUUID": "k1", "Warehouse": "Main", "Quantity": 3},
                            {"ItemUUID": "k1", "Warehouse": "Other", "Quantity": 100}], "c3"),
        }

    def test_products_and_cursors(self):
        res = f1_products.run(self.d)
        self.assertEqual(res["changed"], 2)
        by_code = {p["INT_Balance_Code"]: p for p in self.d.zoho.products}
        self.assertEqual(by_code["A-1"]["Unit_Price"], 12.5)        # configured price type only
        self.assertEqual(by_code["A-1"]["INT_Stock"], 3.0)          # configured warehouse only
        self.assertTrue(by_code["A-1"]["Product_Active"])
        self.assertFalse(by_code["A-2"]["Product_Active"])          # deletion mark -> inactive
        self.assertEqual(self.d.repo.cursors["balance:Stocks"], "c3")

    def test_failed_record_is_logged_and_does_not_block(self):
        self.d.zoho.fail_codes = {"A-1"}
        res = f1_products.run(self.d)
        self.assertEqual((res["ok"], res["failed"]), (1, 1))
        self.assertTrue(any(l["status"] == "failed" and l["entity_ref"] == "A-1" for l in self.d.repo.logs))

    def test_no_changes_no_zoho_calls(self):
        self.d.balance.pages = {}
        f1_products.run(self.d)
        self.assertEqual(self.d.zoho.products, [])


class TestF3F4(unittest.TestCase):
    def setUp(self):
        self.d = make_deps()
        self.d.repo.templates = [
            dict(template_key="contract_prep", trigger_stage="Contracting", clickup_list="L1",
                 name_pattern="ხელშეკრულება: {deal_name}", assignee_ids=["42"], due_days=2),
            dict(template_key="vehicle_prep", trigger_stage="Closed Deal", clickup_list="L2",
                 name_pattern="ტექნიკის მომზადება: {deal_name}", assignee_ids=[], due_days=5),
            dict(template_key="vehicle_handover", trigger_stage="Closed Deal", clickup_list="L2",
                 name_pattern="ტექნიკის გადაცემა: {deal_name}", assignee_ids=[], due_days=7),
        ]
        self.d.zoho.deals["D1"] = {"id": "D1", "Deal_Name": "Sinotruk ×2", "Amount": 100000,
                                   "Account_Name": {"id": "A1", "name": "შპს ტესტი"}, "Stage": "Closed Deal"}

    def test_f3_creates_tasks_once(self):
        f3_deal_stage.run(self.d, "D1", "Closed Deal")
        f3_deal_stage.run(self.d, "D1", "Closed Deal")  # replay
        self.assertEqual(len(self.d.clickup.created), 2)
        names = [b["name"] for _, b in self.d.clickup.created]
        self.assertIn("ტექნიკის მომზადება: Sinotruk ×2", names)
        last = self.d.zoho.deal_updates[-1][1]
        self.assertEqual(last["INT_Execution_Status"], "არ დაწყებულა")
        self.assertEqual(len(last["INT_ClickUp_Task_IDs"].split(",")), 2)
        # link fields are written on each task
        self.assertTrue(any(fid == "cf_deal" and v == "D1" for _, fid, v in self.d.clickup.fields_set))

    def test_f3_assignees_and_due(self):
        f3_deal_stage.run(self.d, "D1", "Contracting")
        body = self.d.clickup.created[0][1]
        self.assertEqual(body["assignees"], [42])
        self.assertIn("due_date", body)

    def test_f4_status_aggregation(self):
        f3_deal_stage.run(self.d, "D1", "Closed Deal")
        t1, t2 = self.d.repo.tasks_of("deal_task", "D1")
        self.assertEqual(f4_task_status.run(self.d, t1)["status"], "არ დაწყებულა")
        self.d.clickup.tasks[t1]["status"] = {"status": "შესრულების პროცესში", "type": "custom"}
        self.assertEqual(f4_task_status.run(self.d, t1)["status"], "მიმდინარე")
        self.d.clickup.close(t1, date(2026, 10, 13))
        self.assertEqual(f4_task_status.run(self.d, t1)["status"], "მიმდინარე")
        self.d.clickup.close(t2, date(2026, 10, 14))
        self.assertEqual(f4_task_status.run(self.d, t2)["status"], "შესრულებულია")
        # Stage is never written
        self.assertTrue(all("Stage" not in f for _, f in self.d.zoho.deal_updates))

    def test_f4_unlinked_task_skipped(self):
        self.assertIsNone(f4_task_status.run(self.d, "nope")["status"])


class TestF2(unittest.TestCase):
    def test_debt_written_and_closed_deals_marked_paid(self):
        d = make_deps()
        d.zoho.accounts["204567890"] = {"id": "A1"}
        d.zoho.deals["D1"] = {"id": "D1", "Account_Name": {"id": "A1"}, "Stage": "Closed Deal"}
        d.zoho.deals["D2"] = {"id": "D2", "Account_Name": {"id": "A1"}, "Stage": "Negotiation"}
        d.repo.id_add("deal_task", "D1", "T9", "vehicle_prep")
        d.clickup.tasks["T9"] = {"id": "T9", "custom_fields": [{"id": "cf_paid", "name": "გადახდილია"}],
                                 "status": {"type": "open"}}

        d.balance.full_rows["ClientsBalances"] = [{"IdentificationCode": "204567890", "Debt": "5000"}]
        f2_balances.run(d)
        self.assertEqual(d.zoho.account_updates[-1], ("A1", {"INT_Balance_Debt": 5000.0}))
        self.assertFalse(d.zoho.deals["D1"].get("INT_Paid"))

        d.balance.full_rows["ClientsBalances"] = [{"IdentificationCode": "204567890", "Debt": "0"}]
        res = f2_balances.run(d)
        self.assertEqual(res["deals_paid"], 1)
        self.assertTrue(d.zoho.deals["D1"]["INT_Paid"])
        self.assertNotIn("INT_Paid", d.zoho.deals["D2"])
        self.assertIn(("T9", "cf_paid", True), d.clickup.fields_set)

        # unchanged debt -> nothing is sent again
        n = len(d.zoho.account_updates)
        f2_balances.run(d)
        self.assertEqual(len(d.zoho.account_updates), n)

    def test_unknown_account_skipped(self):
        d = make_deps()
        d.balance.full_rows["ClientsBalances"] = [{"IdentificationCode": "1", "Debt": "10"}]
        f2_balances.run(d)
        self.assertTrue(any(l["status"] == "skipped" for l in d.repo.logs))


def contract_task(**fields) -> dict:
    names = {
        "number": f7.F_NUMBER, "amount": f7.F_AMOUNT, "contract_date": f7.F_CONTRACT_DATE,
        "port_date": f7.F_PORT_DATE, "paid15": f7.F_PAID15_DATE, "term": f7.F_TERM_80,
    }
    cfs = []
    for key, name in names.items():
        cf = {"id": f"cf_{key}", "name": name, "type": "text"}
        if key in fields:
            cf["value"] = fields[key]
        if key == "amount":
            cf["type"] = "currency"
            cf["type_config"] = {"currency_type": "USD"}
        cfs.append(cf)
    cfs.append({"id": "cf_sup", "name": f7.F_SUPPLIER, "type": "drop_down", "value": 0,
                "type_config": {"options": [{"id": "o1", "name": "Sinotruk", "orderindex": 0}]}})
    return {"id": "C1", "name": "კონტრაქტი 17", "status": {"type": "open"}, "custom_fields": cfs}


class TestF7(unittest.TestCase):
    def test_plan(self):
        c = {"amount": Decimal("100000"), "contract_date": date(2026, 10, 1), "port_date": None,
             "paid15_date": date(2026, 12, 1), "term_80": 70}
        p = {x["key"]: x for x in f7.plan_payments(c)}
        self.assertEqual(p["pay_05"]["amount"], Decimal("5000.00"))
        self.assertEqual(p["pay_05"]["due"], date(2026, 10, 8))
        self.assertEqual(p["pay_05"]["remind"], date(2026, 10, 6))
        self.assertIsNone(p["pay_15"]["due"])
        self.assertEqual(p["pay_80"]["amount"], Decimal("80000.00"))
        self.assertEqual(p["pay_80"]["due"], date(2027, 1, 30))      # +60
        self.assertEqual(p["pay_80"]["remind"], date(2027, 1, 20))   # 10 days before

    def test_custom_term(self):
        c = {"amount": Decimal("1000"), "contract_date": None, "port_date": None,
             "paid15_date": date(2026, 12, 1), "term_80": 90}
        p80 = f7.plan_payments(c)[2]
        self.assertEqual(p80["due"], date(2027, 2, 19))               # 90 - 10 = 80 days

    def test_rounding(self):
        c = {"amount": Decimal("33333.33"), "contract_date": None, "port_date": None,
             "paid15_date": None, "term_80": 70}
        total = sum(x["amount"] for x in f7.plan_payments(c))
        self.assertLessEqual(abs(total - Decimal("33333.33")), Decimal("0.02"))

    def test_parse_contract(self):
        c = f7.parse_contract(contract_task(number="17", amount="250000", contract_date=ms_of(date(2026, 10, 1))))
        self.assertEqual(c["supplier"], "Sinotruk")
        self.assertEqual(c["amount"], Decimal("250000"))
        self.assertEqual(c["currency"], "USD")
        self.assertEqual(c["contract_date"], date(2026, 10, 1))
        self.assertEqual(c["term_80"], 70)

    def test_full_cycle(self):
        d = make_deps()
        d.clickup.tasks["C1"] = contract_task(number="17", amount="100000",
                                              contract_date=ms_of(date(2026, 10, 1)))
        r = f7.on_task_event(d, "C1")
        self.assertEqual(len(r["created"]), 3)
        names = sorted(t["name"] for t in d.clickup.tasks.values() if t.get("parent") == "C1")
        self.assertIn("80% გადახდა — №17 — 80,000.00 USD", names)

        # replay: nothing new, nothing changed
        r = f7.on_task_event(d, "C1")
        self.assertEqual((r["created"], r["updated"]), ([], []))

        # port date set -> 15% gets a due date
        for cf in d.clickup.tasks["C1"]["custom_fields"]:
            if cf["name"] == f7.F_PORT_DATE:
                cf["value"] = ms_of(date(2026, 11, 20))
        r = f7.on_task_event(d, "C1")
        self.assertEqual(len(r["updated"]), 1)

        # 15% subtask closed by a person -> paid15 date written -> 80% scheduled +60
        sub15 = d.repo.id_get("payment", "C1", "pay_15")
        d.clickup.close(sub15, date(2026, 11, 25))
        f7.on_task_event(d, sub15)
        self.assertIn(("C1", "cf_paid15", int(ms_of(date(2026, 11, 25)))), d.clickup.fields_set)
        sub80 = d.repo.id_get("payment", "C1", "pay_80")
        pay80 = d.repo.payments[sub80]
        self.assertEqual(pay80["due_date"], date(2027, 1, 24))
        self.assertEqual(pay80["remind_date"], date(2027, 1, 14))
        self.assertTrue(d.repo.payments[sub15]["paid"])
        self.assertEqual(d.repo.payments[sub15]["paid_date"], date(2026, 11, 25))

    def test_reminders(self):
        d = make_deps(today=date(2027, 1, 14))
        d.repo.payments["S80"] = {"clickup_id": "S80", "step": "pay_80", "contract_no": "17",
                                  "amount": Decimal("80000"), "currency": "USD", "paid": False,
                                  "due_date": date(2027, 1, 24), "remind_date": date(2027, 1, 14)}
        self.assertEqual(f7.send_reminders(d), 1)
        self.assertIn("80%", d.clickup.comments[0][1])


class TestF5(unittest.TestCase):
    def test_sales_month_chunks(self):
        d = make_deps()
        d.balance.sales = [{"Date": "2026-01-15T00:00:00", "ItemUUID": "k1", "Quantity": 1, "Amount": 10,
                            "Document": "N1"},
                           {"Date": "2026-03-02", "ItemUUID": "k1", "Quantity": 2, "Amount": 20, "Document": "N2"}]
        n = f5_dwh.load_sales_report(d, date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(n, 2)
        self.assertEqual({r["sale_date"] for r in d.repo.sales.values()}, {"2026-01-15", "2026-03-02"})

    def test_month_end(self):
        self.assertEqual(f5_dwh._month_end(date(2026, 2, 10)), date(2026, 2, 28))
        self.assertEqual(f5_dwh._month_end(date(2026, 12, 31)), date(2026, 12, 31))

    def test_map_deal(self):
        r = f5_dwh.map_deal({"id": "D1", "Deal_Name": "x", "Account_Name": {"id": "A1"},
                             "Owner": {"name": "Ana"}, "WIN_Code": "W1"}, {"A1": "204"}, "WIN_Code")
        self.assertEqual((r["account_code"], r["owner"], r["win_code"]), ("204", "Ana", "W1"))


class TestOps(unittest.TestCase):
    def test_failed_event_stays_pending_then_succeeds(self):
        d = make_deps()
        d.repo.templates = [dict(template_key="contract_prep", trigger_stage="Contracting", clickup_list="L1",
                                 name_pattern="{deal_name}", assignee_ids=[], due_days=None)]
        d.repo.insert_event("zoho", "D9:Contracting", "F3", {"deal_id": "D9", "stage": "Contracting"})
        ops_jobs.handle_event(d, "zoho", "D9:Contracting", "F3", {"deal_id": "D9", "stage": "Contracting"})
        self.assertEqual(len(d.repo.pending_events()), 1)           # deal not found -> pending
        d.zoho.deals["D9"] = {"id": "D9", "Deal_Name": "ok"}
        self.assertEqual(ops_jobs.process_inbound(d), 1)
        self.assertEqual(len(d.repo.pending_events()), 0)
        self.assertEqual(len(d.clickup.created), 1)

    def test_alert_throttled(self):
        from datetime import datetime, timedelta
        d = make_deps(clickup_incidents_list_id="L_INC")
        d.repo.alert_stats = lambda: {"failed_1h": 11, "last_f1_ok": d.now() - timedelta(minutes=5)}
        self.assertTrue(ops_jobs.check_alerts(d))
        self.assertFalse(ops_jobs.check_alerts(d))                   # same problem, no second task
        self.assertEqual(len(d.clickup.created), 1)

    def test_no_alert_when_healthy(self):
        from datetime import timedelta
        d = make_deps()
        d.repo.alert_stats = lambda: {"failed_1h": 0, "last_f1_ok": d.now() - timedelta(minutes=10)}
        self.assertFalse(ops_jobs.check_alerts(d))

    def test_disabled_flow(self):
        d = make_deps()
        d.settings.flows_enabled["F3"] = False
        self.assertEqual(ops_jobs.dispatch(d, "F3", {"deal_id": "x", "stage": "y"}), {"disabled": "F3"})
        d.settings.flows_enabled["F3"] = True


if __name__ == "__main__":
    unittest.main()
