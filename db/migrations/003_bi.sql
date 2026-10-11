-- ===== bi: the only objects Power BI reads =====
-- Reports depend on these views, never on raw dwh tables, so dwh can change safely.
CREATE SCHEMA IF NOT EXISTS bi;

-- Deal -> execution: days from closing to the last finished task.
CREATE VIEW bi.deal_execution AS
SELECT d.zoho_id,
       d.name,
       d.owner,
       d.amount,
       d.closing_date,
       d.exec_status,
       count(t.clickup_id)                        AS tasks_total,
       count(t.date_done)                         AS tasks_done,
       CASE WHEN count(t.clickup_id) > 0 AND count(t.date_done) = count(t.clickup_id)
            THEN max(t.date_done)::date - d.closing_date END AS days_to_execute
FROM dwh.deal d
LEFT JOIN dwh.task t ON t.zoho_deal_id = d.zoho_id
WHERE d.stage = 'Closed Deal'
GROUP BY d.zoho_id, d.name, d.owner, d.amount, d.closing_date, d.exec_status;

-- Closed deals whose client still has debt in Balance.
CREATE VIEW bi.unpaid_deals AS
SELECT d.zoho_id, d.name, d.owner, d.amount, d.closing_date,
       cb.client_code, cb.client_name, cb.debt, cb.currency
FROM dwh.deal d
JOIN dwh.client_balance cb ON cb.client_code = d.account_code
WHERE d.stage = 'Closed Deal' AND cb.debt > 0;

-- Dead stock: items in stock with no recent sales.
CREATE VIEW bi.slow_stock AS
SELECT i.balance_key, i.code, i.name, i.group_name,
       s.qty,
       ls.last_sale,
       current_date - ls.last_sale AS days_without_sale
FROM dwh.item i
JOIN (SELECT balance_key, sum(qty) AS qty FROM dwh.stock GROUP BY balance_key HAVING sum(qty) > 0) s
     ON s.balance_key = i.balance_key
LEFT JOIN (SELECT balance_key, max(sale_date) AS last_sale FROM dwh.sale GROUP BY balance_key) ls
     ON ls.balance_key = i.balance_key;

-- Sales by month (reports 4 and 6: year-over-year by month / by model).
CREATE VIEW bi.sales_monthly AS
SELECT extract(year  FROM s.sale_date)::int AS year,
       extract(month FROM s.sale_date)::int AS month_no,
       i.group_name,
       i.model,
       s.warehouse,
       sum(s.qty)    AS qty,
       sum(s.amount) AS amount
FROM dwh.sale s
LEFT JOIN dwh.item i USING (balance_key)
GROUP BY 1, 2, 3, 4, 5;

-- Stock by warehouse with value at the configured price type (reports 1 and 2).
CREATE VIEW bi.stock_by_warehouse AS
SELECT s.warehouse, i.group_name, i.model, i.code, i.name,
       s.qty,
       p.price,
       s.qty * coalesce(p.price, 0) AS value
FROM dwh.stock s
JOIN dwh.item i USING (balance_key)
LEFT JOIN LATERAL (
    SELECT price FROM dwh.price pr
    WHERE pr.balance_key = s.balance_key
    ORDER BY pr.updated_at DESC LIMIT 1
) p ON true;

-- Supplier payment calendar (F7).
CREATE VIEW bi.supplier_payments AS
SELECT contract_id, contract_no, supplier, step, amount, currency, due_date, remind_date, paid, paid_date,
       CASE WHEN NOT paid AND due_date < current_date THEN true ELSE false END AS overdue,
       due_date - current_date AS days_left
FROM dwh.supplier_payment;
