-- Consumer grants on the platform objects.  Run as the platform migration identity
-- (platform_migrate), which owns the schema and therefore grants everything below itself;
-- role creation and role attributes live in bootstrap.sql (cluster identity only).
-- Run through apply.sh after views.sql; idempotent.

GRANT USAGE ON SCHEMA :"platform_schema" TO platform_service;
-- Controlled writes on the business objects, append-only on the event log.
GRANT SELECT, INSERT, UPDATE ON
    :"platform_schema".admission, :"platform_schema".request, :"platform_schema".run, :"platform_schema".stage, :"platform_schema".operation
    TO platform_service;
GRANT SELECT, INSERT ON :"platform_schema".run_event TO platform_service;
-- Canonical facts are immutable except for the bounded execution claim lifecycle.
GRANT SELECT, INSERT ON
    :"platform_schema".run_admission, :"platform_schema".artifact, :"platform_schema".command,
    :"platform_schema".command_input_artifact, :"platform_schema".stage_decision,
    :"platform_schema".stage_candidate, :"platform_schema".effect_intent,
    :"platform_schema".effect_fact
    TO platform_service;
GRANT SELECT, INSERT, UPDATE ON :"platform_schema".execution_claim TO platform_service;
GRANT SELECT, INSERT, UPDATE ON :"platform_schema".run_display_metadata TO platform_service;
-- The store's read functions consume the same two read models.
GRANT SELECT ON :"platform_schema".lifecycle_projection, :"platform_schema".admission_projection TO platform_service;

-- operator_ro: exactly the two read models; nothing else in the database.
REVOKE ALL ON SCHEMA :"platform_schema" FROM PUBLIC;
GRANT USAGE ON SCHEMA :"platform_schema" TO operator_ro;
GRANT SELECT ON :"platform_schema".lifecycle_projection, :"platform_schema".admission_projection TO operator_ro;
