-- Operational read models for real scheduling evidence and the admission gate.
--
-- Business run/stage/effect state is projected once by the shared Python
-- contracts in the platform fact store. This view deliberately
-- carries only raw Airflow task/dag-run state and the exact retained keys used
-- by the drain and stop gates.

CREATE OR REPLACE VIEW :"platform_schema".lifecycle_projection AS
WITH bound_claims AS (
    SELECT
        claim.claim_id,
        command.run_id,
        claim.dag_id,
        claim.dag_run_id,
        claim.task_id,
        claim.map_index,
        claim.last_seen_at,
        task.state AS task_state,
        COALESCE(task.end_date, task.start_date, claim.last_seen_at) AS updated_at
    FROM :"platform_schema".execution_claim AS claim
    JOIN :"platform_schema".command AS command
      ON command.command_id = claim.command_id
    LEFT JOIN :"airflow_schema".task_instance AS task
      ON task.dag_id = claim.dag_id
     AND task.task_id = claim.task_id
     AND task.run_id = claim.dag_run_id
     AND task.map_index = claim.map_index
    WHERE claim.dag_id IS NOT NULL
       OR claim.dag_run_id IS NOT NULL
       OR claim.task_id IS NOT NULL
       OR claim.map_index IS NOT NULL
),
scheduling_projection AS (
    SELECT
        bound.claim_id,
        bound.run_id,
        bound.task_state AS state,
        CASE
            WHEN bound.task_state::text = ANY (ARRAY[
                'queued'::text, 'running'::text, 'scheduled'::text,
                'deferred'::text, 'up_for_retry'::text,
                'up_for_reschedule'::text, 'restarting'::text
            ]) THEN 'in_flight'
            WHEN bound.task_state::text = ANY (ARRAY[
                'success'::text, 'failed'::text, 'skipped'::text,
                'upstream_failed'::text, 'removed'::text
            ]) THEN 'terminal'
            ELSE 'unknown'
        END AS bucket,
        bound.updated_at
    FROM bound_claims AS bound
),
dag_run_projection AS (
    SELECT
        dag.dag_id || '/' || dag.run_id AS object_id,
        command.run_id,
        dag.state,
        CASE
            WHEN dag.state::text = ANY (ARRAY[
                'success'::text, 'failed'::text
            ]) THEN 'terminal'
            WHEN dag.state::text = ANY (ARRAY[
                'queued'::text, 'running'::text
            ]) THEN 'in_flight'
            ELSE 'unknown'
        END AS bucket,
        COALESCE(dag.end_date, dag.start_date) AS updated_at
    FROM :"airflow_schema".dag_run AS dag
    JOIN :"platform_schema".execution_claim AS claim
      ON claim.dag_id = dag.dag_id
     AND claim.dag_run_id = dag.run_id
    JOIN :"platform_schema".command AS command
      ON command.command_id = claim.command_id
    GROUP BY
        dag.dag_id,
        dag.run_id,
        dag.state,
        dag.start_date,
        dag.end_date,
        command.run_id
)
SELECT object_kind, object_id, run_id, state, bucket, updated_at
FROM (
    SELECT
        'scheduling'::text AS object_kind,
        claim_id AS object_id,
        run_id,
        state,
        bucket,
        updated_at
    FROM scheduling_projection
    UNION ALL
    SELECT
        'dag_run'::text,
        object_id,
        run_id,
        state,
        bucket,
        updated_at
    FROM dag_run_projection
) AS rows;

CREATE OR REPLACE VIEW :"platform_schema".admission_projection AS
SELECT state, reason, actor, changed_at, revision
FROM :"platform_schema".admission
WHERE id = 1;
