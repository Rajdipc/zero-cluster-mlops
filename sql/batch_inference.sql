-- ==============================================================================
-- Idempotency, statement 2 of 2: push-down ML.PREDICT scoring.
--
-- Single-statement DML, so job.slot_millis, job.total_bytes_billed and
-- job.num_dml_affected_rows are all reliably populated.
--
-- trip_id is carried through ML.PREDICT as a passthrough column: BigQuery ML
-- returns every column of the input subquery alongside the prediction columns.
-- Without it, predictions could not be joined back to the trips they describe,
-- and the output table would be write-only.
--
-- scoring_date is written from the target date parameter (NOT from
-- CURRENT_TIMESTAMP), so a backfill lands in the partition it belongs to.
-- ==============================================================================

INSERT INTO `{project_id}.{dataset_id}.{predictions_table}` (
  trip_id,
  scoring_date,
  vendor_id,
  predicted_is_high_tip,
  predicted_is_high_tip_probs,
  model_name,
  scored_at
)
SELECT
  trip_id,
  {target_date_sql} AS scoring_date,   -- the logical date that was scored
  vendor_id,
  predicted_is_high_tip,
  predicted_is_high_tip_probs,
  '{model_name}' AS model_name,        -- lineage across retrains
  CURRENT_TIMESTAMP() AS scored_at     -- audit only
FROM ML.PREDICT(
  MODEL `{project_id}.{dataset_id}.{model_name}`,
  (
    SELECT
      trip_id,                          -- passthrough join key, not a feature
      vendor_id,
      passenger_count,
      trip_distance,
      fare_amount
      -- total_amount excluded: it contains the label. It is also not knowable
      -- at prediction time, which is the tell that it never belonged here.
    FROM `{project_id}.{dataset_id}.{feature_view}`
    -- Partition filter pruning guarantees no full-table scan.
    WHERE scoring_date = {target_date_sql}
  )
);
