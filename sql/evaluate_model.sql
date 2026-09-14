-- ==============================================================================
-- Continuous Evaluation: measure the model against REALIZED OUTCOMES
--
-- This evaluates the target scoring partition, whose labels have already
-- matured, rather than a frozen holdout table.
--
-- WHY THIS MATTERS: evaluating a static holdout on every run produces a
-- bit-identical metric every single day, because neither the model nor the
-- holdout changes between runs. That timeseries is a flat line and can never
-- alert on anything real. Evaluating against fresh labeled production data
-- turns ROC-AUC into a genuine model-decay signal.
--
-- LABEL LATENCY: taxi tips are known at trip completion, so labels for the
-- target partition are available immediately. In domains where labels lag
-- (churn, fraud chargebacks, credit default), evaluate a trailing window
-- instead -- see EVAL_LABEL_LAG_DAYS in the configuration.
-- ==============================================================================

SELECT
  precision,
  recall,
  accuracy,
  f1_score,
  log_loss,
  roc_auc
FROM ML.EVALUATE(
  MODEL `{project_id}.{dataset_id}.{model_name}`,
  (
    SELECT
      vendor_id,
      passenger_count,
      trip_distance,
      fare_amount,
      is_high_tip
    FROM `{project_id}.{dataset_id}.{feature_view}`
    WHERE scoring_date = {eval_date_sql}
      AND is_high_tip IS NOT NULL
  )
);
