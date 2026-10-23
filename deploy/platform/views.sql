-- Read models for the operator: exactly two views, six columns.
-- Run through apply.sh, which supplies :airflow_schema (default: airflow).
--
-- lifecycle_projection:
--   object_kind in (request, run, stage, operation, scheduling, dag_run).  state is the raw source
--   value - NULL included, never pre-filtered.  bucket is computed here (the single owner of
--   the interpretation) from the per-object vocabulary; NULL or an unrecognized value always
--   becomes unknown.
--   scheduling rows: the retained side is the platform dispatch intent / business binding
--   (operation kinds dispatch/native/publish/cancel), LEFT JOINed to the real Airflow
--   task_instance on the full unique key (dag_id, task_id, run_id, map_index).  A missing
--   binding key never matches - equality against NULL matches nothing, and no key is ever
--   manufactured (no COALESCE): the retained row stays visible with state NULL / bucket
--   unknown (no INNER JOIN, no silent loss).  Only an explicit map_index = -1 (the real
--   unmapped-task value) can bind.  A cancel operation with no binding fields at all is a
--   platform control command, not a scheduler side effect; it remains visible as its
--   terminal operation row and never fabricates a scheduling row.  A partially bound cancel
--   stays schedulable so the missing key remains visible as unknown.  For a present source
--   row updated_at freezes to end_date, falling back to start_date.
--   dag_run rows: one per DagRun referenced by a platform operation (the same binding the
--   scheduling rows use), carrying the real dag_run.state raw - the settledness evidence of
--   the actual Airflow schedule on the same association.  A live DagRun is activity for the
--   gates, so an orphan binding can never claim a settled failure while its own DagRun is
--   still in flight.
--   request rows are accepted facts for reports only; the stop/drain gates ignore the kind.

CREATE OR REPLACE VIEW :"platform_schema".lifecycle_projection AS
SELECT object_kind, object_id, run_id, state, bucket, updated_at
FROM (
    SELECT 'request'::text AS object_kind,
           request_id AS object_id,
           NULL::text AS run_id,
           'accepted'::text AS state,
           'terminal'::text AS bucket,
           sealed_at AS updated_at
    FROM :"platform_schema".request
    UNION ALL
    SELECT 'run'::text,
           run_id,
           run_id,
           state,
           CASE
               WHEN state IN ('queued', 'running') THEN 'in_flight'
               WHEN state IN ('succeeded', 'failed', 'cancelled') THEN 'terminal'
               WHEN state = 'unresolved' THEN 'unresolved'
               ELSE 'unknown'
           END,
           updated_at
    FROM :"platform_schema".run
    UNION ALL
    SELECT 'stage'::text,
           run_id || ':' || stage,
           run_id,
           state,
           CASE
               WHEN state IN ('pending', 'running') THEN 'in_flight'
               WHEN state IN ('completed', 'failed', 'cancelled', 'blocked') THEN 'terminal'
               WHEN state = 'unresolved' THEN 'unresolved'
               ELSE 'unknown'
           END,
           updated_at
    FROM :"platform_schema".stage
    UNION ALL
    SELECT 'operation'::text,
           operation_id,
           run_id,
           state,
           CASE
               WHEN state IN ('queued', 'running') THEN 'in_flight'
               WHEN state IN ('succeeded', 'failed', 'cancelled') THEN 'terminal'
               WHEN state = 'unresolved' THEN 'unresolved'
               ELSE 'unknown'
           END,
           updated_at
    FROM :"platform_schema".operation
    UNION ALL
    SELECT 'scheduling'::text,
           o.operation_id,
           o.run_id,
           ti.state,
           CASE
               WHEN ti.state IN ('queued', 'running', 'scheduled', 'deferred', 'up_for_retry', 'up_for_reschedule', 'restarting')
                   THEN 'in_flight'
               WHEN ti.state IN ('success', 'failed', 'skipped', 'upstream_failed', 'removed') THEN 'terminal'
               ELSE 'unknown'
           END,
           COALESCE(ti.end_date, ti.start_date)
    FROM :"platform_schema".operation o
    LEFT JOIN :"airflow_schema".task_instance ti
           ON ti.dag_id = o.dag_id
          AND ti.task_id = o.task_id
          AND ti.run_id = o.dag_run_id
          AND ti.map_index = o.map_index
    WHERE o.kind IN ('dispatch', 'native', 'publish', 'cancel')
      AND (
          o.kind <> 'cancel'
          OR o.dag_id IS NOT NULL
          OR o.dag_run_id IS NOT NULL
          OR o.task_id IS NOT NULL
          OR o.map_index IS NOT NULL
      )
    UNION ALL
    SELECT 'dag_run'::text,
           dr.dag_id || '/' || dr.run_id,
           dr.run_id,
           dr.state,
           CASE
               WHEN dr.state IN ('success', 'failed') THEN 'terminal'
               WHEN dr.state IN ('queued', 'running') THEN 'in_flight'
               ELSE 'unknown'
           END,
           COALESCE(dr.end_date, dr.start_date)
    FROM :"airflow_schema".dag_run dr
    WHERE EXISTS (
        SELECT 1
        FROM :"platform_schema".operation o
        WHERE o.dag_id = dr.dag_id AND o.dag_run_id = dr.run_id
    )
) AS rows;

CREATE OR REPLACE VIEW :"platform_schema".admission_projection AS
SELECT state, reason, actor, changed_at, revision
FROM :"platform_schema".admission
WHERE id = 1;
