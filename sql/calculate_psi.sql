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
-- ------------------------------------------------------------------------------
-- Bin membership. The first and last bins are OPEN-ENDED, and that matters more
-- than it looks.
--
-- The bin edges come from the BASELINE's deciles, so they span only the range
-- the baseline happened to cover. With closed edges, a scoring row outside that
-- range matches no bin at all: it disappears from the per-bin counts while
-- still being counted in total_s, the denominator. Every actual_pct shrinks
-- slightly and PSI goes DOWN.
--
-- That is exactly backwards. A fare distribution shifting outside its historical
-- range is the single most obvious kind of drift -- a pricing change, a new
-- surcharge, a units bug -- and closed bins make the detector quieter precisely
-- as the drift gets worse. Modelled against these thresholds, 30% of a partition
-- could land outside the baseline range and still score 0.107, comfortably under
-- the 0.25 halt.
--
-- Open edges send those rows to bin 0 or bin 9, where they inflate that bin's
-- actual_pct and raise PSI, which is the behaviour the metric is supposed to have.
--
--   bin 0 : (-inf, p10)   -- everything below the baseline minimum
--   bin 1-8: [p_n, p_n+1) -- half-open, so no row is counted twice
--   bin 9 : [p90, +inf)   -- everything at or above the baseline maximum
--
-- The bins remain mutually exclusive and collectively exhaustive, so every
-- non-NULL row lands in exactly one.
-- ------------------------------------------------------------------------------
baseline_counts AS (
  SELECT b.bin_id, COUNT(1) AS cnt
  FROM baseline_data d
  JOIN bins b
    ON (d.feature_val >= b.min_val OR b.bin_id = 0)
   AND (d.feature_val <  b.max_val OR b.bin_id = 9)
  GROUP BY b.bin_id
),
scoring_counts AS (
  SELECT b.bin_id, COUNT(1) AS cnt
  FROM scoring_data d
  JOIN bins b
    ON (d.feature_val >= b.min_val OR b.bin_id = 0)
   AND (d.feature_val <  b.max_val OR b.bin_id = 9)
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
