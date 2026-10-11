-- ===== ops: integration service tables =====
CREATE SCHEMA IF NOT EXISTS ops;

-- Links between records in different systems.
--   entity = 'deal_task' : zoho_id = Zoho Deal id,           clickup_id = execution task
--   entity = 'payment'   : zoho_id = ClickUp contract task id, clickup_id = payment subtask
--   template_key         : which template / payment step created the row
CREATE TABLE ops.id_map (
    id           bigserial PRIMARY KEY,
    entity       text NOT NULL,
    zoho_id      text,
    clickup_id   text,
    balance_key  text,
    template_key text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entity, zoho_id, template_key),
    UNIQUE (entity, clickup_id)
);
CREATE INDEX id_map_clickup_idx ON ops.id_map (clickup_id);

-- Incremental read cursors ('balance:Items', 'zoho:Deals', 'clickup:tasks', ...).
CREATE TABLE ops.sync_state (
    source       text PRIMARY KEY,
    cursor_value text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Inbound webhook events: deduplication + durable queue.
-- An event is inserted before the HTTP response; processed_at is set when its flow succeeds.
-- process_inbound() re-runs events that stayed unprocessed (e.g. the process restarted).
CREATE TABLE ops.inbound_event (
    source       text NOT NULL,            -- 'zoho' | 'clickup' | 'clickup_contracts'
    event_key    text NOT NULL,
    flow         text NOT NULL,            -- 'F3' | 'F4' | 'F7'
    payload      jsonb NOT NULL,
    attempts     int  NOT NULL DEFAULT 0,
    last_error   text,
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    PRIMARY KEY (source, event_key)
);
CREATE INDEX inbound_event_pending_idx ON ops.inbound_event (received_at) WHERE processed_at IS NULL;

-- Journal of every operation.
CREATE TABLE ops.sync_log (
    id         bigserial PRIMARY KEY,
    flow       text NOT NULL,              -- 'F1'..'F7'
    entity_ref text,
    status     text NOT NULL CHECK (status IN ('ok', 'retry', 'failed', 'skipped')),
    attempts   int  NOT NULL DEFAULT 1,
    error      text,
    payload    jsonb,
    retried_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sync_log_created_idx ON ops.sync_log (created_at);
CREATE INDEX sync_log_failed_idx  ON ops.sync_log (flow, created_at) WHERE status = 'failed';

-- ClickUp task templates per deal stage (list ids live here, not in code).
CREATE TABLE ops.task_template (
    template_key  text PRIMARY KEY,
    trigger_stage text NOT NULL,           -- 'Contracting' | 'Closed Deal'
    clickup_list  text NOT NULL,
    name_pattern  text NOT NULL,           -- e.g. 'ხელშეკრულება: {deal_name}'
    assignee_ids  text[] NOT NULL DEFAULT '{}',
    due_days      int,
    active        boolean NOT NULL DEFAULT true
);
