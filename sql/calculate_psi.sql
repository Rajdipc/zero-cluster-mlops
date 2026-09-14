-- ==============================================================================
-- Population Stability Index (PSI): push-down decile drift calculation
--
-- Compares the target scoring partition against a fixed BASELINE WINDOW of the
-- same unified feature table (rather than a physically separate baseline table).
--
-- Interpretation convention:
--   PSI < 0.10  -> no meaningful shift
--   0.10 - 0.25 -> moderate shift, investigate
--   PSI > 0.25  -> significant shift, halt scoring
-- ==============================================================================

WITH baseline_data AS (
  SELECT {feature_name} AS feature_val
  FROM `{project_id}.{dataset_id}.{feature_view}`
  WHERE scoring_date BETWEEN DATE('{baseline_start_date}') AND DATE('{baseline_end_date}')
    AND {feature_name} IS NOT NULL
),
scoring_data AS (
  SELECT {feature_name} AS feature_val
  FROM `{project_id}.{dataset_id}.{feature_view}`
  WHERE scoring_date = {target_date_sql}
    AND {feature_name} IS NOT NULL
),
quantiles AS (
  SELECT percentiles
  FROM (
    SELECT APPROX_QUANTILES(feature_val, 10) AS percentiles
    FROM baseline_data
  )
),
bins AS (
  SELECT
    offset AS bin_id,
    percentiles[OFFSET(offset)] AS min_val,
    percentiles[OFFSET(offset + 1)] AS max_val
  FROM quantiles, UNNEST(GENERATE_ARRAY(0, 9)) AS offset
),
baseline_counts AS (
  SELECT b.bin_id, COUNT(1) AS cnt
  FROM baseline_data d
  JOIN bins b
    ON d.feature_val >= b.min_val
   AND (d.feature_val < b.max_val OR (b.bin_id = 9 AND d.feature_val <= b.max_val))
  GROUP BY b.bin_id
),
scoring_counts AS (
  SELECT b.bin_id, COUNT(1) AS cnt
  FROM scoring_data d
  JOIN bins b
    ON d.feature_val >= b.min_val
   AND (d.feature_val < b.max_val OR (b.bin_id = 9 AND d.feature_val <= b.max_val))
  GROUP BY b.bin_id
),
total_counts AS (
  SELECT
    (SELECT COUNT(1) FROM baseline_data) AS total_b,
    (SELECT COUNT(1) FROM scoring_data)  AS total_s
),
distributions AS (
  SELECT
    b.bin_id,
    -- Laplace smoothing (0.0001) prevents division by zero / undefined ln()
    -- when a quantile bin is empty on either side.
    COALESCE(bc.cnt / NULLIF(tc.total_b, 0), 0.0001) AS expected_pct,
    COALESCE(sc.cnt / NULLIF(tc.total_s, 0), 0.0001) AS actual_pct
  FROM bins b
  CROSS JOIN total_counts tc
  LEFT JOIN baseline_counts bc ON b.bin_id = bc.bin_id
  LEFT JOIN scoring_counts sc  ON b.bin_id = sc.bin_id
)
SELECT
  ROUND(SUM((actual_pct - expected_pct) * LN(actual_pct / expected_pct)), 4) AS total_psi
FROM distributions;
