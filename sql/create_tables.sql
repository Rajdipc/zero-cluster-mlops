-- ==============================================================================
-- Migration DDL: Idempotent schemas for the Zero-Cluster BQML Pipeline
--
-- NOTE ON JOB TYPE: This file is intentionally multi-statement and is executed
-- ONLY by scripts/seed_and_train.sh (a migration step), never by the runtime
-- orchestrator. Multi-statement SQL runs as a BigQuery *script job*, where
-- num_dml_affected_rows is NULL. Runtime queries are kept strictly
-- single-statement so their job statistics stay reliable.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Unified feature table.
--
-- A single partitioned table holds every role (training window, PSI baseline
-- window, and daily scoring partitions). The role is selected by date range
-- rather than by physically separate tables. This removes three-way schema
-- duplication and guarantees training and inference read identical columns.
--
-- trip_id is a synthesized surrogate key. The public NYC TLC dataset has no
-- primary key, so without one it is impossible to join a prediction back to
-- the trip it describes.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `{project_id}.{dataset_id}.{features_table}` (
  trip_id STRING NOT NULL OPTIONS(description = "Synthesized surrogate key (MD5 of natural trip attributes)."),
  scoring_date DATE NOT NULL OPTIONS(description = "Logical partition date. Drives training/baseline/scoring windows."),
  pickup_datetime TIMESTAMP,
  dropoff_datetime TIMESTAMP,
  vendor_id STRING,
  passenger_count INT64,
  trip_distance FLOAT64,
  fare_amount FLOAT64,
  total_amount FLOAT64,
  tip_amount FLOAT64,
  payment_type STRING OPTIONS(description = "TLC payment code. '1' = credit card, '2' = cash. NYC TLC does NOT record cash tips, so only '1' carries a trustworthy label. Stored here rather than filtered at ingestion so the exclusion stays visible and auditable in the canonical view."),
  is_high_tip INT64 OPTIONS(description = "Binary label: 1 when tip_amount > 2.00. Meaningful ONLY for payment_type = '1' -- see v_taxi_features. NULL when the label has not matured.")
)
PARTITION BY scoring_date
CLUSTER BY vendor_id
OPTIONS(
  description = "Unified feature store for taxi tipping propensity. Partitioned by logical date; role determined by date window."
);

-- ------------------------------------------------------------------------------
-- 2. Canonical feature contract.
--
-- This view is the SINGLE source of truth for the feature column list AND the
-- data-quality predicates. Training, evaluation, and inference all read through
-- it, so the three cannot drift apart. This is the structural defense against
-- training/serving skew.
--
-- Deliberately NOT using a TRANSFORM clause: BigQuery ML already standardizes
-- numeric features and one-hot-encodes categoricals for LOGISTIC_REG, so custom
-- preprocessing would be redundant here. A TRANSFORM clause would also tighten
-- the ML.PREDICT input contract, complicating passthrough of trip_id.
--
-- WHY total_amount IS NOT A FEATURE (this is the important part)
--
--   total_amount = fare_amount + extra + mta_tax + tip_amount
--                  + tolls_amount + imp_surcharge + airport_fee
--
-- The label is `tip_amount > 2.00`, and total_amount CONTAINS tip_amount. Using
-- it as a feature is textbook target leakage: the model is handed the answer.
--
-- Measured on 1,121,626 real trips (2022-01-01..01-15), this single rule
--
--     (total_amount - fare_amount) > 5.5
--
-- classifies the label with 88.8% accuracy. A model given both columns learns
-- that subtraction and reports a superb ROC-AUC that collapses in production --
-- where total_amount is not even known until AFTER the trip is paid for, which
-- is strictly after the moment you wanted the prediction.
--
-- total_amount is kept as a COLUMN on the base table (it is real data, useful
-- for auditing and for the trip_id hash) but excluded from this view, which is
-- the feature contract. Excluding it here removes it from training, evaluation
-- and inference at once -- that is exactly what a single contract buys you.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE VIEW `{project_id}.{dataset_id}.{feature_view}` AS
SELECT
  trip_id,
  scoring_date,
  pickup_datetime,
  vendor_id,
  passenger_count,
  trip_distance,
  fare_amount,
  -- total_amount deliberately absent -- see the leakage note above.
  is_high_tip
FROM `{project_id}.{dataset_id}.{features_table}`
WHERE vendor_id IS NOT NULL
  AND passenger_count > 0
  AND trip_distance > 0
  AND fare_amount BETWEEN 2.50 AND 100.00
  -- total_amount appears here as a DATA QUALITY predicate only. Filtering on a
  -- column is not the same as learning from it: this drops corrupt rows without
  -- exposing the value to the model.
  AND total_amount > 0
  -- Credit card only. This is a LABEL VALIDITY filter, not a data-cleaning one,
  -- and it is the most consequential line in this file.
  --
  -- NYC TLC records tip_amount only for card payments. Cash tips are always
  -- written as 0.00 -- not because the passenger did not tip, but because the
  -- meter never saw it. In the January 2022 training window that is 22% of
  -- rows, every one labelled is_high_tip = 0 no matter how long or expensive
  -- the trip was.
  --
  -- Keeping them would teach the model that a fifth of perfectly ordinary trips
  -- produce no tip, with nothing in the feature set able to explain why.
  -- (payment_type is deliberately NOT a feature: including it would just let
  -- the model memorise "cash implies zero" and learn nothing about tipping.)
  -- Excluding them lifts corr(trip_distance, label) from 0.19 to 0.258.
  --
  -- The honest restatement of the task: "GIVEN A CARD PAYMENT, will the tip
  -- exceed $2.00?" That is also the only version the dispatching use case can
  -- actually act on.
  AND payment_type = '1';

-- ------------------------------------------------------------------------------
-- 3. Predictions table.
--
-- Partitioned by scoring_date (the date of the data that was scored), NOT by
-- scored_at (the wall-clock time the job ran). Partitioning on wall-clock time
-- would place a backfill of 2022-02-10 into today's partition, making the row
-- unattributable to its source data.
--
-- scored_at is retained as a non-partitioning audit column.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `{project_id}.{dataset_id}.{predictions_table}` (
  trip_id STRING NOT NULL OPTIONS(description = "Join key back to the feature table."),
  scoring_date DATE NOT NULL OPTIONS(description = "Logical date of the data that was scored."),
  vendor_id STRING,
  predicted_is_high_tip INT64,
  predicted_is_high_tip_probs ARRAY<STRUCT<label INT64, prob FLOAT64>>
    OPTIONS(description = "Calibrated probabilities. Classes are near-balanced (~48/52) so no class weighting is applied, which keeps these usable for expected-value math."),
  model_name STRING OPTIONS(description = "Model that produced this row, for lineage across retrains."),
  scored_at TIMESTAMP OPTIONS(description = "Audit column: wall-clock execution time. Not the partition key.")
)
PARTITION BY scoring_date
CLUSTER BY vendor_id
OPTIONS(
  description = "Batch inference output. Partitioned by the logical date scored so backfills are idempotent and attributable."
);
