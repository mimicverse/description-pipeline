-- Consumer grants on the canonical platform objects. Run as the platform
-- migration identity (platform_migrate), which owns the schema and therefore
-- grants everything below itself. Role creation and attributes live in bootstrap.sql.

GRANT USAGE ON SCHEMA :"platform_schema" TO platform_service;

-- Admission is the one mutable control row. Canonical pipeline facts are immutable
-- except for the bounded execution-claim lifecycle and display-only metadata.
GRANT SELECT, INSERT, UPDATE ON :"platform_schema".admission TO platform_service;
GRANT SELECT, INSERT ON
    :"platform_schema".run_admission,
    :"platform_schema".artifact,
    :"platform_schema".command,
    :"platform_schema".command_input_artifact,
    :"platform_schema".stage_decision,
    :"platform_schema".stage_candidate,
    :"platform_schema".effect_intent,
    :"platform_schema".effect_fact
    TO platform_service;
GRANT SELECT, INSERT, UPDATE ON
    :"platform_schema".execution_claim,
    :"platform_schema".run_display_metadata
    TO platform_service;
GRANT SELECT ON
    :"platform_schema".lifecycle_projection,
    :"platform_schema".admission_projection
    TO platform_service;

-- operator_ro sees exactly the two read models and no base table.
REVOKE ALL ON SCHEMA :"platform_schema" FROM PUBLIC;
GRANT USAGE ON SCHEMA :"platform_schema" TO operator_ro;
GRANT SELECT ON
    :"platform_schema".lifecycle_projection,
    :"platform_schema".admission_projection
    TO operator_ro;
