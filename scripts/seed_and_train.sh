#!/usr/bin/env bash
# ==============================================================================
# Seed BigQuery tables from the NYC TLC public dataset and train the BQML model.
#
# Idempotent: safe to re-run. Tables are replaced, not appended to.
# ==============================================================================
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo '')}"
DATASET_ID="${BQ_DATASET_ID:-ml_production}"
MODEL_NAME="${MODEL_NAME:-taxi_tip_model}"
FEATURES_TABLE="${FEATURES_TABLE:-taxi_trips_features}"
FEATURE_VIEW="${FEATURE_VIEW:-v_taxi_features}"
PREDICTIONS_TABLE="${PREDICTIONS_TABLE:-taxi_predictions}"

# BigQuery location is NOT the Cloud Run region.
#
# bigquery-public-data lives in the US multi-region, and BigQuery cannot join
# across locations. Creating this dataset in us-central1 makes the seed query
# below fail with a location mismatch -- the single most common first-run error
# in BigQuery tutorials. Leave this as US.
BQ_LOCATION="${BQ_LOCATION:-US}"

# Date windows. Training and PSI baseline share one window; two further
# partitions are seeded so you can exercise a scheduled run and a backfill.
TRAIN_START="${TRAIN_START_DATE:-2022-01-01}"
TRAIN_END="${TRAIN_END_DATE:-2022-01-15}"
BACKFILL_DATE="${BACKFILL_DATE:-2022-02-10}"
SCORING_SOURCE_DATE="${SCORING_SOURCE_DATE:-2022-02-01}"

# The public dataset stores one table per year: tlc_yellow_trips_YYYY.
# Verified populated 2011-2022 with an identical 20-column schema.
# WARNING: the 2023 table exists but is EMPTY (0 rows, 19 columns). If you change
# the year here, change the *_DATE variables above to match or you will seed an
# empty table and every later step will fail for a reason that looks unrelated.
SOURCE_TABLE="${DEMO_SOURCE_TABLE:-bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: GCP_PROJECT_ID is not set." >&2
  echo "       export GCP_PROJECT_ID=your-project-id" >&2
  echo "       (or run: gcloud config set project <ID>)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_DIR="$(cd "${SCRIPT_DIR}/../sql" && pwd)"

echo "========================================================================"
echo "Seeding Zero-Cluster BQML environment"
echo "  Project         : ${PROJECT_ID}"
echo "  Dataset         : ${DATASET_ID}"
echo "  BQ location     : ${BQ_LOCATION}   (must be US for public data joins)"
echo "  Model           : ${MODEL_NAME}"
echo "  Training window : ${TRAIN_START} .. ${TRAIN_END}"
echo "========================================================================"

run_query() {
  bq query --use_legacy_sql=false --location="${BQ_LOCATION}" \
           --project_id="${PROJECT_ID}" "$1"
}

render() {
  sed -e "s|{project_id}|${PROJECT_ID}|g" \
      -e "s|{dataset_id}|${DATASET_ID}|g" \
      -e "s|{model_name}|${MODEL_NAME}|g" \
      -e "s|{features_table}|${FEATURES_TABLE}|g" \
      -e "s|{feature_view}|${FEATURE_VIEW}|g" \
      -e "s|{predictions_table}|${PREDICTIONS_TABLE}|g" \
      -e "s|{train_start_date}|${TRAIN_START}|g" \
      -e "s|{train_end_date}|${TRAIN_END}|g" \
      "$1"
}

# ------------------------------------------------------------------------------
echo
echo "[1/4] Ensuring dataset '${DATASET_ID}' exists in location '${BQ_LOCATION}'..."
if bq --location="${BQ_LOCATION}" show --dataset "${PROJECT_ID}:${DATASET_ID}" >/dev/null 2>&1; then
  echo "      Dataset already exists."
else
  bq --location="${BQ_LOCATION}" mk -d \
     --description="Zero-cluster BQML production dataset" \
     "${PROJECT_ID}:${DATASET_ID}"
  echo "      Created."
fi

# ------------------------------------------------------------------------------
echo
echo "[2/4] Creating feature table, canonical feature view and predictions table..."
run_query "$(render "${SQL_DIR}/create_tables.sql")"

# ------------------------------------------------------------------------------
# NOTE: the public table column is vendor_id (snake_case), not VendorID.
# trip_id is synthesized because the public dataset has no primary key; without
# it, predictions cannot be joined back to the trips they describe.
echo
echo "[3/4] Loading feature partitions from bigquery-public-data..."
run_query "
CREATE TEMP FUNCTION make_trip_id(
  pickup TIMESTAMP, dropoff TIMESTAMP, vendor STRING, dist FLOAT64, total FLOAT64
) AS (
  TO_HEX(MD5(FORMAT('%t|%t|%s|%t|%t', pickup, dropoff, IFNULL(vendor,''), dist, total)))
);

CREATE OR REPLACE TABLE \`${PROJECT_ID}.${DATASET_ID}.${FEATURES_TABLE}\`
PARTITION BY scoring_date
CLUSTER BY vendor_id
AS
WITH raw AS (
  SELECT
    pickup_datetime,
    dropoff_datetime,
    CAST(vendor_id AS STRING) AS vendor_id,
    passenger_count,
    trip_distance,
    fare_amount,
    total_amount,
    tip_amount,
    -- Required by the canonical view's label-validity filter. TLC records tips
    -- only for card payments ('1'); cash tips are always written as 0.00.
    CAST(payment_type AS STRING) AS payment_type,
    DATE(pickup_datetime) AS source_date
  FROM \`${SOURCE_TABLE}\`
  WHERE DATE(pickup_datetime) BETWEEN '${TRAIN_START}' AND '${TRAIN_END}'
     OR DATE(pickup_datetime) = '${SCORING_SOURCE_DATE}'
     OR DATE(pickup_datetime) = '${BACKFILL_DATE}'
)
SELECT
  make_trip_id(pickup_datetime, dropoff_datetime, vendor_id, trip_distance, total_amount) AS trip_id,
  -- Training and baseline partitions keep their true date. The partition that
  -- stands in for 'today's arriving data' is remapped to yesterday so the
  -- default scheduled run has something to score.
  CASE
    WHEN source_date = DATE('${SCORING_SOURCE_DATE}')
      THEN DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    ELSE source_date
  END AS scoring_date,
  pickup_datetime,
  dropoff_datetime,
  vendor_id,
  passenger_count,
  trip_distance,
  fare_amount,
  total_amount,
  tip_amount,
  payment_type,
  IF(tip_amount > 2.0, 1, 0) AS is_high_tip
FROM raw
WHERE vendor_id IS NOT NULL
  AND passenger_count > 0
  AND trip_distance > 0
  AND fare_amount BETWEEN 2.50 AND 100.00
  AND total_amount > 0
-- The synthesized key must be unique within a partition, otherwise the
-- 'rows == unique_trips' idempotency assertion in the README is meaningless.
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY
    make_trip_id(pickup_datetime, dropoff_datetime, vendor_id, trip_distance, total_amount),
    CASE
      WHEN source_date = DATE('${SCORING_SOURCE_DATE}')
        THEN DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
      ELSE source_date
    END
) = 1;
"

echo
echo "      Partition summary:"
bq query --use_legacy_sql=false --location="${BQ_LOCATION}" \
         --project_id="${PROJECT_ID}" --format=pretty "
SELECT scoring_date, COUNT(1) AS rows, COUNTIF(is_high_tip = 1) AS high_tip
FROM \`${PROJECT_ID}.${DATASET_ID}.${FEATURES_TABLE}\`
GROUP BY scoring_date ORDER BY scoring_date
"

# ------------------------------------------------------------------------------
echo
echo "[4/4] Training BQML logistic regression model '${MODEL_NAME}'..."
echo "      (chronological SEQ split; this typically takes 1-3 minutes)"
run_query "$(render "${SQL_DIR}/train_model.sql")"

echo
echo "========================================================================"
echo "Seed complete."
echo "  Feature table : ${PROJECT_ID}.${DATASET_ID}.${FEATURES_TABLE}"
echo "  Feature view  : ${PROJECT_ID}.${DATASET_ID}.${FEATURE_VIEW}"
echo "  Predictions   : ${PROJECT_ID}.${DATASET_ID}.${PREDICTIONS_TABLE}"
echo "  Model         : ${PROJECT_ID}.${DATASET_ID}.${MODEL_NAME}"
echo
echo "Next: build and push the container, then apply Terraform."
echo "========================================================================"
