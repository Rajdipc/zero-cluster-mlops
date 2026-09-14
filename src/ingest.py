"""Phase 0: demo-only synthesis of an arriving data partition.

> In a real deployment this module should be deleted.

Populating the feature table belongs to your upstream ETL, not to the scoring
pipeline that consumes it. This exists solely so the public-dataset demo keeps
working past day one -- the seed script plants a single "yesterday" partition
frozen at seed time, so without this the scheduled job would correctly fail
every subsequent night.

Set ``ENABLE_DEMO_INGESTION=false`` for anything real. The pipeline then simply
reads whatever your ETL has landed, and a missing partition raises
``InsufficientDataException`` as it should.
"""

import logging
from typing import Dict

from google.cloud import bigquery
from opentelemetry import trace

from src.config import PipelineConfig
from src.telemetry import record_job_stats

logger = logging.getLogger("bqml_orchestrator.ingest")
tracer = trace.get_tracer(__name__)


def run_demo_ingestion(client: bigquery.Client, config: PipelineConfig) -> Dict[str, int]:
    """Ensures the target partition holds data, synthesizing it if absent.

    No-op when demo mode is disabled, and no-op when the partition is already
    populated -- the idempotency guard lives inside the SQL statement, so a
    skip and an ingest are distinguished by ``num_dml_affected_rows``.

    Returns:
        FinOps counters, with ``rows_affected`` = 0 when skipped.
    """
    if not config.enable_demo_ingestion:
        logger.info(
            "Demo ingestion disabled; expecting upstream ETL to have populated "
            f"partition {config.resolved_target_date}."
        )
        return {"slot_millis": 0, "bytes_billed": 0, "rows_affected": 0}

    with tracer.start_as_current_span("ingest.demo_partition") as span:
        span.set_attribute("ingest.demo_mode", True)
        span.set_attribute("ingest.target_date", config.resolved_target_date)
        span.set_attribute(
            "ingest.source_window",
            f"{config.demo_source_window_start} +{config.demo_source_window_days}d",
        )

        logger.warning(
            "DEMO MODE: synthesizing feature partition "
            f"{config.resolved_target_date} from the public dataset. "
            "Disable with ENABLE_DEMO_INGESTION=false in any real deployment."
        )

        job = client.query(config.load_sql("ingest_demo_partition.sql"))
        job.result()
        stats = record_job_stats(job, config, "ingest")

        if stats["rows_affected"] == 0:
            logger.info(
                f"Partition {config.resolved_target_date} already populated; "
                "ingestion skipped (idempotent no-op)."
            )
            span.set_attribute("ingest.skipped", True)
        else:
            logger.info(
                f"Ingested {stats['rows_affected']:,} rows into partition "
                f"{config.resolved_target_date}."
            )
            span.set_attribute("ingest.skipped", False)

        return stats
