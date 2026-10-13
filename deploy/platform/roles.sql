-- Role boundary (A v3.3).  Run through apply.sh after views.sql; :airflow_schema defaults to
-- airflow.  PostgreSQL views execute with owner privileges, so operator_ro reads the
-- scheduling facts without any privilege on the Airflow metadata schema.
--
--   platform_owner    owns the schema, the tables and the two views; reads the real Airflow
--                     metadata for the view definition.
--   platform_service  the application-side store role: controlled writes on the platform
--                     objects only.
--   operator_ro       the operatorctl role: SELECT on exactly the two read models.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_owner') THEN
        CREATE ROLE platform_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_service') THEN
        CREATE ROLE platform_service LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'operator_ro') THEN
        CREATE ROLE operator_ro LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'airflow_runtime') THEN
        CREATE ROLE airflow_runtime LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'airflow_migrate') THEN
        CREATE ROLE airflow_migrate LOGIN;
    END IF;
END $$;

-- Role attributes are enforced idempotently as well: the roles are cluster-scoped and may
-- already exist from an earlier definition.
ALTER ROLE platform_owner NOLOGIN;
ALTER ROLE platform_service LOGIN;
ALTER ROLE operator_ro LOGIN;
-- The Airflow runtime identity: least privilege, no DDL anywhere.  Its grants on the airflow
-- schema are applied right after the migration (operatorctl install), because the schema does
-- not exist yet when this file runs.
ALTER ROLE airflow_runtime LOGIN;
ALTER ROLE airflow_migrate LOGIN;
-- Both Airflow roles resolve unqualified metadata names (including the migration ledger)
-- inside the airflow schema only.
ALTER ROLE airflow_runtime IN DATABASE :"platform_database" SET search_path = airflow, pg_catalog;
ALTER ROLE airflow_migrate IN DATABASE :"platform_database" SET search_path = airflow, pg_catalog;

GRANT USAGE ON SCHEMA :"airflow_schema" TO platform_owner;
GRANT SELECT ON :"airflow_schema".task_instance TO platform_owner;

ALTER SCHEMA platform OWNER TO platform_owner;
ALTER TABLE platform.admission OWNER TO platform_owner;
ALTER TABLE platform.request OWNER TO platform_owner;
ALTER TABLE platform.run OWNER TO platform_owner;
ALTER TABLE platform.stage OWNER TO platform_owner;
ALTER TABLE platform.operation OWNER TO platform_owner;
ALTER VIEW platform.lifecycle_projection OWNER TO platform_owner;
ALTER VIEW platform.admission_projection OWNER TO platform_owner;

GRANT USAGE ON SCHEMA platform TO platform_service;
GRANT SELECT, INSERT, UPDATE ON
    platform.admission, platform.request, platform.run, platform.stage, platform.operation
    TO platform_service;
-- The event log is append-only by privilege: no UPDATE/DELETE is ever granted.
GRANT SELECT, INSERT ON platform.run_event TO platform_service;
-- The store's read functions consume the same two read models.
GRANT SELECT ON platform.lifecycle_projection, platform.admission_projection TO platform_service;

REVOKE ALL ON SCHEMA platform FROM PUBLIC;
GRANT USAGE ON SCHEMA platform TO operator_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA platform FROM operator_ro;
GRANT SELECT ON platform.lifecycle_projection, platform.admission_projection TO operator_ro;
