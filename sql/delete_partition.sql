-- ==============================================================================
-- Idempotency, statement 1 of 2: clear the target partition.
--
-- Runs as its OWN BigQuery job, deliberately NOT combined with the INSERT.
--
-- Combining them into one file would make BigQuery compile a multi-statement
-- SCRIPT job, where num_dml_affected_rows is NULL and job.destination points at
-- an ephemeral _script_result_ table -- destroying exactly the telemetry this
-- pipeline exists to collect. Two single-statement jobs cost one extra round
-- trip and keep slot_millis / bytes_billed / rows_affected reliable for both.
--
-- Partition pruning on scoring_date makes this a metadata-light operation.
-- ==============================================================================

DELETE FROM `{project_id}.{dataset_id}.{predictions_table}`
WHERE scoring_date = {target_date_sql};
