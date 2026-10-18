-- Airflow-side grants for the platform boundary and the Airflow runtime identity.
--
-- Run through apply.sh --airflow-grants AFTER `airflow db migrate` and BEFORE the platform
-- apply: the platform views read :"airflow_schema".task_instance and .dag_run under owner
-- privileges, and the runtime identity stays DML-only inside its own schema.  Executed with
-- the installation identity (superuser); run as the platform migration role nothing here is
-- in reach.
ALTER ROLE airflow_runtime LOGIN;
GRANT USAGE ON SCHEMA :"airflow_schema" TO platform_migrate;
GRANT SELECT ON :"airflow_schema".task_instance TO platform_migrate;
GRANT SELECT ON :"airflow_schema".dag_run TO platform_migrate;
-- The write-side reconcile reads the original Airflow scheduling facts for the
-- admitted closure: exactly the identity/state/terminal-attempt columns it binds, nothing else.
GRANT USAGE ON SCHEMA :"airflow_schema" TO platform_service;
GRANT SELECT (dag_id, run_id, task_id, map_index, state, try_number)
    ON :"airflow_schema".task_instance TO platform_service;
GRANT SELECT (dag_id, run_id, state) ON :"airflow_schema".dag_run TO platform_service;
GRANT USAGE ON SCHEMA :"airflow_schema" TO airflow_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA :"airflow_schema" TO airflow_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA :"airflow_schema" TO airflow_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA :"airflow_schema" TO airflow_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE airflow_migrate IN SCHEMA :"airflow_schema"
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO airflow_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE airflow_migrate IN SCHEMA :"airflow_schema"
    GRANT USAGE, SELECT ON SEQUENCES TO airflow_runtime;
