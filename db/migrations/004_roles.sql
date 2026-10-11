-- ===== Read-only role for Power BI =====
-- The password is set separately (see README), never stored in git:
--   ALTER ROLE pbi_reader PASSWORD '...';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pbi_reader') THEN
        CREATE ROLE pbi_reader LOGIN;
    END IF;
END $$;

GRANT USAGE  ON SCHEMA bi, dwh TO pbi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA bi, dwh TO pbi_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA bi  GRANT SELECT ON TABLES TO pbi_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA dwh GRANT SELECT ON TABLES TO pbi_reader;
