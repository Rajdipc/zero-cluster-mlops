-- ==============================================================================
-- Training: BQML Logistic Regression with chronological split
--
-- Reads through the canonical feature view so the training feature set and
-- filter predicates are byte-identical to what inference will use.
--
-- data_split_method='SEQ' (not AUTO_SPLIT): trips are time-ordered, so a random
-- split would place future trips in the training set and past trips in eval,
-- leaking information forward and producing an optimistically biased ROC-AUC
-- that will not hold in production, where you always predict forward in time.
--
-- No TRANSFORM clause: BigQuery ML automatically standardizes numeric features
-- and one-hot-encodes categorical features for LOGISTIC_REG, so explicit
-- scaling here would be redundant. See sql/create_tables.sql for the full
-- rationale and README.md for when TRANSFORM *is* the right tool.
--
-- Note what is NOT in the SELECT below: total_amount. It contains tip_amount,
-- and the label is derived from tip_amount, so including it leaks the target.
-- The exclusion is enforced by the feature view, not by this file -- see the
-- extended note in sql/create_tables.sql.
--
-- NOTE: auto_class_weights is deliberately NOT set.
--
-- The reflex on a binary target is to switch it on. Measured on the actual
-- data, the card-only training window is 61.7% positive -- close to balanced,
-- and nowhere near the skew that would justify reweighting.
--
-- Enabling it would not be free. Class weighting intentionally distorts the
-- decision boundary, which turns the emitted probabilities into ranking
-- scores rather than calibrated posteriors. The motivating use case
-- ("offer a guarantee when P(high tip) x fare > threshold") multiplies that
-- probability by money, so breaking calibration to fix an imbalance that
-- does not exist would be a straight downgrade.
--
-- Porting this to a genuinely rare event -- fraud, churn, default -- is the
-- case where you should revisit it, and calibrate afterwards.
-- ==============================================================================

CREATE OR REPLACE MODEL `{project_id}.{dataset_id}.{model_name}`
OPTIONS (
  model_type = 'LOGISTIC_REG',
  input_label_cols = ['is_high_tip'],

  -- Chronological split: the most recent 20% of the window becomes the eval set.
  data_split_method = 'SEQ',
  data_split_col = 'pickup_datetime',
  data_split_eval_fraction = 0.20,

  model_registry = 'VERTEX_AI',
  vertex_ai_model_id = '{model_name}'
) AS
SELECT
  -- pickup_datetime is required by data_split_col. BQML excludes the split
  -- column from the feature set automatically.
  pickup_datetime,
  vendor_id,
  passenger_count,
  trip_distance,
  fare_amount,
  is_high_tip
FROM `{project_id}.{dataset_id}.{feature_view}`
WHERE scoring_date BETWEEN DATE('{train_start_date}') AND DATE('{train_end_date}')
  AND is_high_tip IS NOT NULL;
