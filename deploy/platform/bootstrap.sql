-- Platform/Airflow bootstrap: the ONLY step that runs with the cluster identity.
-- It creates the roles, the two schemas and the per-role search_path; everything else
-- (schema objects, views, grants to the platform consumers) runs as the platform migration
-- identity (platform_migrate), which owns only the platform schema.  Run through
-- apply.sh --bootstrap; idempotent.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_migrate') THEN
        CREATE ROLE platform_migrate LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_service') THEN
        CREATE ROLE platform_service LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'operator_ro') THEN
        CREATE ROLE operator_ro LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'airflow_migrate') THEN
        CREATE ROLE airflow_migrate LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'airflow_runtime') THEN
        CREATE ROLE airflow_runtime LOGIN;
    END IF;
END $$;

-- The migration identities are never superusers; the platform migrator owns its schema only.
ALTER ROLE platform_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE airflow_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE platform_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE operator_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE airflow_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;

CREATE SCHEMA IF NOT EXISTS platform AUTHORIZATION platform_migrate;
ALTER SCHEMA platform OWNER TO platform_migrate;
CREATE SCHEMA IF NOT EXISTS :"airflow_schema" AUTHORIZATION airflow_migrate;
ALTER SCHEMA :"airflow_schema" OWNER TO airflow_migrate;

-- Unqualified metadata names (including the migration ledger) resolve inside each role's own
-- schema; nothing crosses schemas implicitly.
ALTER ROLE platform_migrate IN DATABASE :"platform_database" SET search_path = platform, pg_catalog;
ALTER ROLE platform_service IN DATABASE :"platform_database" SET search_path = platform, pg_catalog;
ALTER ROLE airflow_migrate IN DATABASE :"platform_database" SET search_path = :"airflow_schema", pg_catalog;
ALTER ROLE airflow_runtime IN DATABASE :"platform_database" SET search_path = :"airflow_schema", pg_catalog;

-- The supported PostgreSQL (14) grants CREATE/usage on the public schema to PUBLIC by default;
-- tighten the effective ACL here, at installation time, so no service or migration role can
-- create objects outside its own schema (revoking from a single role would not remove PUBLIC
-- inheritance).
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON DATABASE :"platform_database" FROM PUBLIC;

-- No ownership rewriting: every object is created by the identity that owns its schema, and a
-- foreign-owned object makes that identity's migration fail loudly instead of being absorbed.
