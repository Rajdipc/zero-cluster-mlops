"""Main pipeline orchestrator executed inside Cloud Run Jobs."""

import logging
import sys
import time

from google.cloud import bigquery
from opentelemetry import trace

from src.config import PipelineConfig
from src.drift import (
    DriftDetectedException,
    InsufficientDataException,
    run_drift_guardrail,
)
from src.evaluate import run_model_evaluation
from src.inference import run_batch_inference
from src.ingest import run_demo_ingestion
from src.run_summary import STATUS_SUCCEEDED, RunSummary
from src.telemetry import setup_telemetry

logger = logging.getLogger("bqml_orchestrator")
tracer = trace.get_tracer(__name__)

# Exit codes. Cloud Run surfaces these as the task result.
EXIT_OK = 0
EXIT_GUARDRAIL_HALT = 2   # Terminal: bad input data. Retrying will not help.
EXIT_UNEXPECTED = 3       # Possibly transient. Safe to retry (writes are idempotent).


def main() -> None:
    """Executes the zero-cluster MLOps batch inference workflow."""
    config = PipelineConfig()
    tracer_provider, meter_provider = setup_telemetry(config)
    exit_code = EXIT_OK
    started = time.monotonic()
    summary = RunSummary(
        target_date=config.resolved_target_date,
        psi_threshold=config.psi_drift_threshold,
    )
    failure: BaseException | None = None

    try:
        # BigQuery location is intentionally separate from the Cloud Run region:
        # the dataset lives in the US multi-region so it can join against
        # bigquery-public-data, while the container runs in a specific region.
        client = bigquery.Client(
            project=config.gcp_project_id,
            location=config.bq_location,
        )

        with tracer.start_as_current_span("orchestrator.pipeline_run") as root_span:
            root_span.set_attribute("pipeline.target_date", config.resolved_target_date)
            root_span.set_attribute("pipeline.dataset_id", config.bq_dataset_id)
            root_span.set_attribute("pipeline.model_name", config.model_name)

            logger.info("=" * 64)
            logger.info("Zero-Cluster MLOps Batch Pipeline")
            logger.info(f"  Target partition : {config.resolved_target_date}")
            logger.info(f"  BigQuery dataset : {config.bq_dataset_id} ({config.bq_location})")
            logger.info(f"  Model            : {config.model_name}")
            logger.info(f"  Demo ingestion   : {config.enable_demo_ingestion}")
            logger.info("=" * 64)

            # Phase 0 is DEMO SCAFFOLDING and is a no-op when disabled.
            # Real deployments rely on upstream ETL to land the partition.
            logger.info("Phase 0/3: Feature partition availability (demo mode only)...")
            run_demo_ingestion(client, config)

            logger.info("Phase 1/3: Pre-flight validation and PSI drift guardrail...")
            summary.psi = run_drift_guardrail(client, config)

            logger.info("Phase 2/3: Continuous evaluation against realized labels...")
            eval_metrics = run_model_evaluation(client, config)
            if eval_metrics:
                summary.roc_auc = eval_metrics.get("roc_auc")

            logger.info("Phase 3/3: Idempotent push-down batch scoring...")
            scored = run_batch_inference(client, config)
            if isinstance(scored, dict):
                summary.rows_scored = scored.get("rows_scored")

            logger.info("Pipeline completed successfully.")
            root_span.set_status(trace.StatusCode.OK)

    except (DriftDetectedException, InsufficientDataException) as exc:
        # Terminal guardrail halt. The input data is bad; an immediate retry
        # would read the same partition and fail identically. Paired with
        # max_retries=0 in Terraform so Cloud Run does not duplicate the run.
        logger.error(f"Guardrail halted the pipeline: {exc}")
        exit_code = EXIT_GUARDRAIL_HALT
        failure = exc

    except Exception as exc:
        # Potentially transient (BigQuery 5xx, quota, network). Distinguished by
        # exit code so retry policy can be reasoned about separately from
        # deliberate guardrail halts. All writes are idempotent, so a retry is
        # safe: the partition is cleared before it is repopulated.
        logger.error(f"Pipeline failed with an unexpected error: {exc}", exc_info=True)
        exit_code = EXIT_UNEXPECTED
        failure = exc

    finally:
        # One human-readable status line per run. The "BQML Batch Run Status"
        # alert policy emails it, so it must be written on every path, and
        # before the flush so it is never lost to the CPU freeze below.
        try:
            fields = summary.build(exit_code, time.monotonic() - started, failure)
            level = logging.INFO if fields["status"] == STATUS_SUCCEEDED else logging.ERROR
            logger.log(level, fields["status_message"], extra={"json_fields": fields})
        except Exception as summary_exc:  # never mask the real exit code
            logger.warning(f"Could not write the run summary: {summary_exc}")

        # CRITICAL: Cloud Run freezes container vCPUs the instant the main
        # thread exits, killing OTel background exporter threads and dropping
        # anything still buffered. shutdown() blocks until spans and metrics
        # have actually been delivered.
        logger.info("Flushing OpenTelemetry trace and metric buffers...")
        try:
            tracer_provider.shutdown()
            meter_provider.shutdown()
        except Exception as flush_exc:  # never mask the real exit code
            logger.warning(f"Error during telemetry shutdown flush: {flush_exc}")

        logger.info(f"Container terminating with exit code {exit_code}")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
