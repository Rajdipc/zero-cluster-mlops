-- ==============================================================================
-- PHASE 0 (DEMO MODE ONLY): synthesize an arriving data partition.
--
-- ============================ READ THIS BEFORE USE ============================
-- In a real deployment this file SHOULD NOT EXIST. Populating the feature table
-- is the job of your upstream ETL / ELT / CDC pipeline, not of the scoring
-- pipeline that reads it. A batch scoring job that also ingests its own input
-- owns two responsibilities and can no longer tell "upstream is late" apart
-- from "upstream is broken".
--
-- It exists only so the public-dataset demo keeps working after day one.
-- The seed script plants a single "yesterday" partition, frozen at seed time;
-- without this phase the scheduled job would correctly fail every subsequent
-- night with InsufficientDataException. Set ENABLE_DEMO_INGESTION=false (and
-- delete this file) for anything real.
-- ==============================================================================
--
-- Two properties make this safe to run unconditionally:
--
--   1. IDEMPOTENT. The NOT EXISTS guard means this is a no-op whenever the
--      target partition already holds data, so a re-run or a backfill of a
--      real partition can never overwrite or duplicate it. Because the guard
--      lives in the statement itself, num_dml_affected_rows reports 0 on skip
--      and N on ingest -- the telemetry distinguishes the two for free.
--
--   2. DETERMINISTIC. The source day is a pure function of the target date, so
--      re-scoring a given date always ingests the same underlying trips.
--      A random or CURRENT_DATE-relative mapping would make backfills
--      irreproducible.
--
-- Single-statement DML, so slot_millis / bytes_billed / num_dml_affected_rows
-- stay reliable (see Gotcha #4 on script jobs).
-- ==============================================================================

INSERT INTO `{project_id}.{dataset_id}.{features_table}` (
  trip_id,
  scoring_date,
  pickup_datetime,
  dropoff_datetime,
  vendor_id,
  passenger_count,
  trip_distance,
  fare_amount,
  total_amount,
  tip_amount,
  payment_type,
  is_high_tip
)
WITH rotated AS (
  -- Map the target date onto a repeating window of real historical days.
  -- MOD over days-since-epoch cycles through the window deterministically.
  SELECT DATE_ADD(
    DATE('{demo_source_window_start}'),
    INTERVAL MOD(
      DATE_DIFF({target_date_sql}, DATE('1970-01-01'), DAY),
      {demo_source_window_days}
    ) DAY
  ) AS source_date
),
candidate AS (
  SELECT
    TO_HEX(MD5(FORMAT('%t|%t|%s|%t|%t',
      t.pickup_datetime,
      t.dropoff_datetime,
      IFNULL(CAST(t.vendor_id AS STRING), ''),
      t.trip_distance,
      t.total_amount
    ))) AS trip_id,
    {target_date_sql} AS scoring_date,
    t.pickup_datetime,
    t.dropoff_datetime,
    CAST(t.vendor_id AS STRING) AS vendor_id,
    t.passenger_count,
    t.trip_distance,
    t.fare_amount,
    t.total_amount,
    t.tip_amount,
    -- Required by the canonical view's label-validity filter; see
    -- sql/create_tables.sql. Cash tips are never recorded by TLC.
    CAST(t.payment_type AS STRING) AS payment_type,
    IF(t.tip_amount > 2.0, 1, 0) AS is_high_tip
  FROM `{demo_source_table}` AS t
  CROSS JOIN rotated r
  WHERE DATE(t.pickup_datetime) = r.source_date
    AND t.vendor_id IS NOT NULL
    AND t.passenger_count > 0
    AND t.trip_distance > 0
    AND t.fare_amount BETWEEN 2.50 AND 100.00
    AND t.total_amount > 0
)
SELECT
  trip_id,
  scoring_date,
  pickup_datetime,
  dropoff_datetime,
  vendor_id,
  passenger_count,
  trip_distance,
  fare_amount,
  total_amount,
  tip_amount,
  payment_type,
  is_high_tip
FROM candidate
-- Idempotency guard: insert nothing if this partition is already populated.
WHERE NOT EXISTS (
  SELECT 1
  FROM `{project_id}.{dataset_id}.{features_table}`
  WHERE scoring_date = {target_date_sql}
)
-- The synthesized key must be unique within the partition, otherwise the
-- rows == unique_trips idempotency assertion becomes meaningless.
QUALIFY ROW_NUMBER() OVER (PARTITION BY trip_id) = 1;
