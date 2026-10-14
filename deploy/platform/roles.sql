-- Consumer grants on the platform objects.  Run as the platform migration identity
-- (platform_migrate), which owns the schema and therefore grants everything below itself;
-- role creation and role attributes live in bootstrap.sql (cluster identity only).
-- Run through apply.sh after views.sql; idempotent.

GRANT USAGE ON SCHEMA platform TO platform_service;
-- Controlled writes on the business objects, append-only on the event log.
GRANT SELECT, INSERT, UPDATE ON
    platform.admission, platform.request, platform.run, platform.stage, platform.operation
    TO platform_service;
GRANT SELECT, INSERT ON platform.run_event TO platform_service;
-- The store's read functions consume the same two read models.
GRANT SELECT ON platform.lifecycle_projection, platform.admission_projection TO platform_service;

-- operator_ro: exactly the two read models; nothing else in the database.
REVOKE ALL ON SCHEMA platform FROM PUBLIC;
GRANT USAGE ON SCHEMA platform TO operator_ro;
GRANT SELECT ON platform.lifecycle_projection, platform.admission_projection TO operator_ro;
