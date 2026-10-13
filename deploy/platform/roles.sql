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
        CREATE ROLE platform_service NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'operator_ro') THEN
        CREATE ROLE operator_ro NOLOGIN;
    END IF;
END $$;

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

REVOKE ALL ON SCHEMA platform FROM PUBLIC;
GRANT USAGE ON SCHEMA platform TO operator_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA platform FROM operator_ro;
GRANT SELECT ON platform.lifecycle_projection, platform.admission_projection TO operator_ro;
