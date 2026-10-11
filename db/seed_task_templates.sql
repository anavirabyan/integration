-- ClickUp task templates per Zoho deal stage.
-- Replace <LIST_ID> with real ClickUp list ids and fill assignee_ids, then run once:
--   psql "$DATABASE_URL" -f db/seed_task_templates.sql
-- Names / lists / assignees / due days are to be confirmed with the operations team.
INSERT INTO ops.task_template (template_key, trigger_stage, clickup_list, name_pattern, assignee_ids, due_days) VALUES
 ('contract_prep',       'Contracting', '<LIST_ID>', 'ხელშეკრულება: {deal_name}',            '{}', 2),
 ('vehicle_prep',        'Closed Deal', '<LIST_ID>', 'ტექნიკის მომზადება: {deal_name}',       '{}', 5),
 ('vehicle_handover',    'Closed Deal', '<LIST_ID>', 'ტექნიკის გადაცემა: {deal_name}',         '{}', 7),
 ('aftersales_handover', 'Closed Deal', '<LIST_ID>', 'After-sales-ზე გადაცემა: {deal_name}',   '{}', 8)
ON CONFLICT (template_key) DO UPDATE
SET trigger_stage = EXCLUDED.trigger_stage,
    clickup_list  = EXCLUDED.clickup_list,
    name_pattern  = EXCLUDED.name_pattern,
    assignee_ids  = EXCLUDED.assignee_ids,
    due_days      = EXCLUDED.due_days;
