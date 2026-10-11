-- ===== dwh: copies of source data for analytics =====
-- Exact Balance columns are confirmed against the JSON descriptions (docs/balance/*.json);
-- `raw` keeps the original payload so no field is lost before it gets its own column.
CREATE SCHEMA IF NOT EXISTS dwh;

CREATE TABLE dwh.item (
    balance_key text PRIMARY KEY,
    code        text,
    name        text,
    group_name  text,
    unit        text,
    model       text,
    active      boolean NOT NULL DEFAULT true,
    raw         jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dwh.price (
    balance_key text NOT NULL,
    price_type  text NOT NULL,
    price       numeric(14,2),
    currency    text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (balance_key, price_type)
);

CREATE TABLE dwh.stock (
    balance_key text NOT NULL,
    warehouse   text NOT NULL,
    qty         numeric(14,3) NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (balance_key, warehouse)
);

-- Daily copy of dwh.stock: stock history for the dead-stock analysis.
CREATE TABLE dwh.stock_snapshot (
    snap_date   date NOT NULL,
    balance_key text NOT NULL,
    warehouse   text NOT NULL,
    qty         numeric(14,3) NOT NULL,
    PRIMARY KEY (snap_date, balance_key, warehouse)
);

CREATE TABLE dwh.client_balance (
    client_code text PRIMARY KEY,           -- identification code, same as Zoho Accounts.Identification_Code
    client_name text,
    debt        numeric(14,2) NOT NULL DEFAULT 0,
    currency    text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dwh.sale (
    row_key     text PRIMARY KEY,
    sale_date   date NOT NULL,
    client_code text,
    balance_key text,
    warehouse   text,
    qty         numeric(14,3),
    amount      numeric(14,2),
    raw         jsonb
);
CREATE INDEX sale_item_date_idx ON dwh.sale (balance_key, sale_date);

CREATE TABLE dwh.deal (
    zoho_id       text PRIMARY KEY,
    name          text,
    stage         text,
    amount        numeric(14,2),
    account_id    text,
    account_code  text,
    owner         text,
    win_code      text,
    closing_date  date,
    paid          boolean,
    exec_status   text,
    created_time  timestamptz,
    modified_time timestamptz
);

CREATE TABLE dwh.task (
    clickup_id   text PRIMARY KEY,
    zoho_deal_id text,
    template_key text,
    name         text,
    status       text,
    status_type  text,
    assignees    text[],
    date_created timestamptz,
    date_done    timestamptz,
    due_date     timestamptz
);

-- Supplier contract payments (F7), copied for the payment calendar report.
CREATE TABLE dwh.supplier_payment (
    clickup_id    text PRIMARY KEY,         -- payment subtask
    contract_id   text NOT NULL,            -- contract task
    contract_no   text,
    supplier      text,
    step          text NOT NULL,            -- 'pay_05' | 'pay_15' | 'pay_80'
    amount        numeric(14,2),
    currency      text,
    due_date      date,
    remind_date   date,
    paid          boolean NOT NULL DEFAULT false,
    paid_date     date,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
