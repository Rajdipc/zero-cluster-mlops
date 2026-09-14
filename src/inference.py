"""In-warehouse push-down batch scoring with idempotent partition replacement."""

import logging
from typing import Dict

from google.cloud import bigquery
from opentelemetry import trace

import src.telemetry as telemetry
from src.config import PipelineConfig
from src.telemetry import record_job_stats

logger = logging.getLogger("bqml_orchestrator.inference")
tracer = trace.get_tracer(__name__)


def run_batch_inference(client: bigquery.Client, config: PipelineConfig) -> Dict[str, int]:
    """Idempotently replaces the target prediction partition.

    Executes two SEPARATE single-statement jobs rather than one multi-statement
    script. Combining them would make BigQuery compile a script job, where
    ``num_dml_affected_rows`` is NULL -- destroying the telemetry this pipeline
    exists to collect.

    Returns:
        Aggregated FinOps counters across both jobs.
    """
    with tracer.start_as_current_span("inference.execute_batch_predict") as span:
        span.set_attribute("inference.model_name", config.model_name)
        span.set_attribute("inference.target_date", config.resolved_target_date)
        span.set_attribute("inference.predictions_table", config.predictions_table)

        # --- Job 1: clear the target partition (idempotency) ------------------
        logger.info(
            f"Clearing any existing predictions for partition "
            f"{config.resolved_target_date} (idempotent re-run safety)..."
        )
        delete_job = client.query(config.load_sql("delete_partition.sql"))
        delete_job.result()
        delete_stats = record_job_stats(delete_job, config, "delete")

        if delete_stats["rows_affected"]:
            logger.info(
                f"Removed {delete_stats['rows_affected']:,} pre-existing rows "
                "(this run is a re-execution or backfill)."
            )

        # --- Job 2: push-down scoring ----------------------------------------
        logger.info(
            f"Triggering push-down ML.PREDICT for partition "
            f"{config.resolved_target_date}..."
        )
        insert_job = client.query(config.load_sql("batch_inference.sql"))
        insert_job.result()
        insert_stats = record_job_stats(insert_job, config, "insert")

        rows_scored = insert_stats["rows_affected"]
        if telemetry.metric_rows_scored is not None:
            telemetry.metric_rows_scored.add(
                rows_scored, attributes={"model_name": config.model_name}
            )

        total = {
            "rows_scored": rows_scored,
            "rows_deleted": delete_stats["rows_affected"],
            "slot_millis": delete_stats["slot_millis"] + insert_stats["slot_millis"],
            "bytes_billed": delete_stats["bytes_billed"] + insert_stats["bytes_billed"],
        }

        span.set_attribute("bq.total_slot_millis", total["slot_millis"])
        span.set_attribute("bq.total_bytes_billed", total["bytes_billed"])
        span.set_attribute("bq.rows_scored", rows_scored)

        logger.info(
            f"Batch inference complete. Rows scored={rows_scored:,}, "
            f"slot_millis={total['slot_millis']:,}, "
            f"bytes_billed={total['bytes_billed']:,} "
            f"(insert job: {insert_job.job_id})"
        )
        return total
