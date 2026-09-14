"""Pre-flight validation and in-warehouse statistical drift guardrail."""

import logging

from google.cloud import bigquery
from opentelemetry import trace

import src.telemetry as telemetry
from src.config import PipelineConfig

logger = logging.getLogger("bqml_orchestrator.drift")
tracer = trace.get_tracer(__name__)


class DriftDetectedException(Exception):
    """Raised when the evaluated Population Stability Index exceeds the threshold."""


class InsufficientDataException(Exception):
    """Raised when the target scoring partition is missing or suspiciously small."""


def run_preflight(client: bigquery.Client, config: PipelineConfig) -> int:
    """Asserts the target partition exists and carries a plausible row count.

    Returns:
        The row count of the target partition.

    Raises:
        InsufficientDataException: If the partition is below ``min_row_count``.
    """
    logger.info(
        f"Pre-flight: checking partition {config.resolved_target_date} "
        f"in {config.features_table}..."
    )

    sql = f"""
    SELECT COUNT(1) AS row_count
    FROM `{config.gcp_project_id}.{config.bq_dataset_id}.{config.feature_view}`
    WHERE scoring_date = {config.target_date_sql}
    """
    row_count = list(client.query(sql).result())[0].row_count

    span = trace.get_current_span()
    span.set_attribute("preflight.row_count", row_count)
    span.set_attribute("preflight.min_row_count", config.min_row_count)

    if row_count < config.min_row_count:
        msg = (
            f"Pre-flight failed: partition {config.resolved_target_date} has "
            f"{row_count:,} rows, below the minimum of {config.min_row_count:,}. "
            "This usually indicates a partially failed or missing upstream load."
        )
        logger.error(msg)
        raise InsufficientDataException(msg)

    logger.info(f"Pre-flight passed: {row_count:,} rows in target partition.")
    return row_count


def run_drift_guardrail(client: bigquery.Client, config: PipelineConfig) -> float:
    """Runs pre-flight checks and the push-down PSI circuit breaker.

    Returns:
        The calculated Population Stability Index.

    Raises:
        InsufficientDataException: If the partition fails pre-flight validation.
        DriftDetectedException: If PSI meets or exceeds the configured threshold.
    """
    with tracer.start_as_current_span("drift.check_and_calculate_psi") as span:
        span.set_attribute("drift.canary_feature", config.canary_feature)
        span.set_attribute("drift.threshold", config.psi_drift_threshold)
        span.set_attribute("drift.target_date", config.resolved_target_date)
        span.set_attribute(
            "drift.baseline_window",
            f"{config.baseline_start_date}..{config.baseline_end_date}",
        )

        run_preflight(client, config)

        logger.info(
            f"Evaluating push-down PSI for canary feature '{config.canary_feature}' "
            f"against baseline window "
            f"{config.baseline_start_date}..{config.baseline_end_date}..."
        )
        rows = list(client.query(config.load_sql("calculate_psi.sql")).result())
        total_psi = float(rows[0].total_psi) if rows and rows[0].total_psi is not None else 0.0

        span.set_attribute("drift.total_psi", total_psi)

        if telemetry.metric_feature_psi is not None:
            telemetry.metric_feature_psi.set(
                total_psi,
                attributes={
                    "feature": config.canary_feature,
                    "model_name": config.model_name,
                },
            )

        if total_psi >= config.psi_drift_threshold:
            msg = (
                f"CRITICAL: Feature drift detected: PSI is {total_psi:.4f} "
                f"(Threshold: {config.psi_drift_threshold})"
            )
            logger.critical(msg)
            span.set_status(trace.StatusCode.ERROR, msg)
            raise DriftDetectedException(msg)

        logger.info(
            f"PSI drift check passed: PSI is {total_psi:.4f} "
            f"(Threshold: {config.psi_drift_threshold})"
        )
        return total_psi
