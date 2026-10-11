-- Database checks: run against an EMPTY test database after the migrations.
--   psql "$TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -f tests/sql/check_db.sql
-- Every check raises an exception when it fails.
BEGIN;

INSERT INTO dwh.item (balance_key, code, name, group_name, model, active) VALUES
 ('k1','A-1','Filter','ნაწილები',NULL,true), ('k2','A-2','Pump','ნაწილები',NULL,true),
 ('k3','T-1','Sinotruk HOWO','ტექნიკა','HOWO T7',true);
INSERT INTO dwh.price (balance_key, price_type, price) VALUES ('k1','Retail',12.5), ('k1','Wholesale',10), ('k3','Retail',90000);
INSERT INTO dwh.stock (balance_key, warehouse, qty) VALUES ('k1','Main',3), ('k1','Other',100), ('k2','Main',5), ('k3','Main',0);
INSERT INTO dwh.sale (row_key, sale_date, balance_key, qty, amount) VALUES
 ('s1', current_date - 400, 'k2', 1, 50), ('s2', current_date - 5, 'k1', 2, 25),
 ('s3', DATE '2025-03-10', 'k3', 1, 85000), ('s4', DATE '2026-03-12', 'k3', 2, 180000);
INSERT INTO dwh.client_balance (client_code, debt) VALUES ('204', 5000), ('205', 0);
INSERT INTO dwh.deal (zoho_id, name, stage, amount, account_code, closing_date) VALUES
 ('D1','Deal 1','Closed Deal',100,'204', current_date - 10),
 ('D2','Deal 2','Closed Deal',200,'205', current_date - 10),
 ('D3','Deal 3','Negotiation',300,'204', NULL);
INSERT INTO dwh.task (clickup_id, zoho_deal_id, date_done) VALUES
 ('t1','D1', now() - interval '2 days'), ('t2','D1', NULL), ('t3','D2', now());
INSERT INTO dwh.supplier_payment (clickup_id, contract_id, contract_no, step, amount, due_date, remind_date, paid) VALUES
 ('p1','C1','17','pay_80',80000, current_date + 10, current_date, false),
 ('p2','C1','17','pay_05', 5000, current_date - 1, current_date - 3, false);

DO $$
DECLARE n int; v numeric; r record;
BEGIN
  -- F1 product_rows (same SQL as Repo.product_rows, parameters inlined)
  SELECT i.code,
         (SELECT price FROM dwh.price p WHERE p.balance_key = i.balance_key
            AND ('Retail' = '' OR p.price_type = 'Retail') ORDER BY updated_at DESC LIMIT 1) AS price,
         (SELECT coalesce(sum(qty), 0) FROM dwh.stock s WHERE s.balance_key = i.balance_key
            AND (cardinality(ARRAY['Main']::text[]) = 0 OR s.warehouse = ANY(ARRAY['Main']::text[]))) AS qty
    INTO r FROM dwh.item i WHERE i.balance_key = ANY(ARRAY['k1']::text[]);
  IF r.price <> 12.5 OR r.qty <> 3 THEN RAISE EXCEPTION 'product_rows: %', r; END IF;

  -- empty warehouse list = all warehouses
  SELECT coalesce(sum(qty),0) INTO v FROM dwh.stock s WHERE s.balance_key='k1'
     AND (cardinality(ARRAY[]::text[]) = 0 OR s.warehouse = ANY(ARRAY[]::text[]));
  IF v <> 103 THEN RAISE EXCEPTION 'all warehouses: %', v; END IF;

  SELECT count(*) INTO n FROM bi.unpaid_deals;
  IF n <> 1 THEN RAISE EXCEPTION 'unpaid_deals: %', n; END IF;

  SELECT * INTO r FROM bi.deal_execution WHERE zoho_id = 'D1';
  IF r.tasks_total <> 2 OR r.tasks_done <> 1 OR r.days_to_execute IS NOT NULL THEN
     RAISE EXCEPTION 'deal_execution D1: %', r; END IF;
  SELECT * INTO r FROM bi.deal_execution WHERE zoho_id = 'D2';
  IF r.days_to_execute <> 10 THEN RAISE EXCEPTION 'deal_execution D2: %', r; END IF;

  SELECT count(*) INTO n FROM bi.slow_stock;               -- k3 has qty 0 -> excluded
  IF n <> 2 THEN RAISE EXCEPTION 'slow_stock rows: %', n; END IF;
  SELECT days_without_sale INTO n FROM bi.slow_stock WHERE balance_key = 'k2';
  IF n <> 400 THEN RAISE EXCEPTION 'slow_stock days: %', n; END IF;

  SELECT sum(amount) INTO v FROM bi.sales_monthly WHERE model = 'HOWO T7' AND month_no = 3;
  IF v <> 265000 THEN RAISE EXCEPTION 'sales_monthly: %', v; END IF;

  SELECT value INTO v FROM bi.stock_by_warehouse WHERE code = 'A-2';
  IF v <> 0 THEN RAISE EXCEPTION 'stock_by_warehouse no price -> 0: %', v; END IF;

  SELECT count(*) INTO n FROM bi.supplier_payments WHERE overdue;
  IF n <> 1 THEN RAISE EXCEPTION 'supplier_payments overdue: %', n; END IF;

  -- Repo.payments_to_remind: "remind_date BETWEEN %s - 3 AND %s" with a date parameter
  SELECT count(*) INTO n FROM dwh.supplier_payment
   WHERE NOT paid AND remind_date BETWEEN current_date - 3 AND current_date;
  IF n <> 2 THEN RAISE EXCEPTION 'payments_to_remind: %', n; END IF;

  -- id_map uniqueness = F3 idempotency at the database level
  INSERT INTO ops.id_map (entity, zoho_id, clickup_id, template_key) VALUES ('deal_task','D1','c1','contract_prep');
  INSERT INTO ops.id_map (entity, zoho_id, clickup_id, template_key) VALUES ('deal_task','D1','c2','contract_prep')
    ON CONFLICT DO NOTHING;
  SELECT count(*) INTO n FROM ops.id_map WHERE zoho_id = 'D1';
  IF n <> 1 THEN RAISE EXCEPTION 'id_map unique: %', n; END IF;

  -- inbound_event dedupe
  INSERT INTO ops.inbound_event (source, event_key, flow, payload) VALUES ('zoho','D1:Contracting','F3','{}');
  INSERT INTO ops.inbound_event (source, event_key, flow, payload) VALUES ('zoho','D1:Contracting','F3','{}')
    ON CONFLICT DO NOTHING;
  SELECT count(*) INTO n FROM ops.inbound_event;
  IF n <> 1 THEN RAISE EXCEPTION 'inbound dedupe: %', n; END IF;

  -- sync_log status check constraint
  BEGIN
    INSERT INTO ops.sync_log (flow, status) VALUES ('F1', 'weird');
    RAISE EXCEPTION 'status constraint missing';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  -- pbi_reader can read bi but not ops
  IF NOT has_table_privilege('pbi_reader', 'bi.unpaid_deals', 'SELECT') THEN RAISE EXCEPTION 'pbi_reader bi'; END IF;
  IF has_table_privilege('pbi_reader', 'ops.sync_log', 'SELECT') THEN RAISE EXCEPTION 'pbi_reader sees ops'; END IF;

  RAISE NOTICE 'ALL DATABASE CHECKS PASSED';
END $$;

ROLLBACK;
