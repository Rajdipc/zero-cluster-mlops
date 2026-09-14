"""OpenTelemetry tracing, Cloud Monitoring metrics, and structured JSON logging."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

from src.config import PipelineConfig

logger = logging.getLogger("bqml_orchestrator")

# Global metric instrument handles
metric_feature_psi = None
metric_slot_millis = None
metric_bytes_billed = None
metric_rows_scored = None
metric_eval_roc_auc = None


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as structured JSON correlated with Cloud Trace."""

    def __init__(self, project_id: str, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.project_id = project_id

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": self._map_severity(record.levelname),
            "message": record.getMessage(),
            "logger": record.name,
            "sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }

        # Correlate with active OpenTelemetry trace context
        current_span = trace.get_current_span()
        if current_span and current_span.get_span_context().is_valid:
            span_ctx = current_span.get_span_context()
            trace_id_hex = f"{span_ctx.trace_id:032x}"
            span_id_hex = f"{span_ctx.span_id:016x}"

            # Google Cloud Logging trace correlation syntax
            log_entry["logging.googleapis.com/trace"] = (
                f"projects/{self.project_id}/traces/{trace_id_hex}"
            )
            log_entry["logging.googleapis.com/spanId"] = span_id_hex
            log_entry["logging.googleapis.com/trace_sampled"] = (
                span_ctx.trace_flags.sampled
            )

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)

    @staticmethod
    def _map_severity(level_name: str) -> str:
        level_map = {
            "DEBUG": "DEBUG",
            "INFO": "INFO",
            "WARNING": "WARNING",
            "ERROR": "ERROR",
            "CRITICAL": "CRITICAL",
        }
        return level_map.get(level_name.upper(), "DEFAULT")


def setup_logging(project_id: str) -> None:
    """Configures root logger with the StructuredJsonFormatter."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter(project_id=project_id))
    root_logger.addHandler(handler)


def setup_telemetry(config: PipelineConfig) -> Tuple[TracerProvider, MeterProvider]:
    """Initializes OpenTelemetry TracerProvider and MeterProvider.

    Synchronously drains all buffers upon container shutdown.
    """
    global metric_feature_psi, metric_slot_millis, metric_bytes_billed, metric_rows_scored, metric_eval_roc_auc

    setup_logging(config.gcp_project_id)

    resource = Resource.create(
        {
            "service.name": "bqml-taxi-batch-worker",
            "service.version": "1.0.0",
            "deployment.environment": "production",
            "gcp.project_id": config.gcp_project_id,
        }
    )

    # 1. Trace Provider Configuration
    tracer_provider = TracerProvider(resource=resource)
    trace_exporter = None

    if config.enable_cloud_exporters:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            trace_exporter = CloudTraceSpanExporter(project_id=config.gcp_project_id)
            logger.info("Initialized Google Cloud Trace span exporter.")
        except Exception as exc:
            logger.warning(
                f"CloudTraceSpanExporter unavailable ({exc}). Falling back to ConsoleSpanExporter."
            )
            trace_exporter = ConsoleSpanExporter()
    else:
        trace_exporter = ConsoleSpanExporter()

    tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tracer_provider)

    # 2. Metrics Provider Configuration
    #
    # Cloud Monitoring rejects two points written to the SAME time series within
    # 5 seconds with INVALID_ARGUMENT. A short periodic interval combined with
    # the shutdown() force-flush reliably trips this on short-lived jobs -- and
    # it fails as a logged export error rather than a crash, so it is silent.
    #
    # Mitigation: keep the periodic interval long (default 60s) so that for a
    # ~25s job the shutdown flush is effectively the only export, and tag each
    # execution with a unique identifier so concurrent or retried runs write to
    # distinct time series instead of colliding.
    interval = config.metric_export_interval_millis
    metric_reader = None

    if config.enable_cloud_exporters:
        try:
            from opentelemetry.exporter.cloud_monitoring import (
                CloudMonitoringMetricsExporter,
            )

            gcp_metric_exporter = CloudMonitoringMetricsExporter(
                project_id=config.gcp_project_id,
                add_unique_identifier=True,
            )
            metric_reader = PeriodicExportingMetricReader(
                gcp_metric_exporter,
                export_interval_millis=interval,
            )
            logger.info(
                f"Initialized Cloud Monitoring metric exporter "
                f"(export_interval={interval}ms, unique_identifier=True)."
            )
        except Exception as exc:
            logger.warning(
                f"CloudMonitoringMetricsExporter unavailable ({exc}). "
                "Falling back to ConsoleMetricExporter."
            )
            metric_reader = PeriodicExportingMetricReader(
                ConsoleMetricExporter(), export_interval_millis=interval
            )
    else:
        metric_reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=interval
        )

    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # 3. Create Metric Instruments
    meter = metrics.get_meter("bqml.batch.worker", "1.0.0")

    metric_feature_psi = meter.create_gauge(
        name="bqml.drift.feature_psi",
        description="Evaluated Population Stability Index (PSI) for canary feature",
        unit="1",
    )
    metric_slot_millis = meter.create_counter(
        name="bqml.inference.slot_millis",
        description="BigQuery compute slot milliseconds consumed by inference",
        unit="ms",
    )
    metric_bytes_billed = meter.create_counter(
        name="bqml.inference.bytes_billed",
        description="BigQuery total bytes billed by inference queries",
        unit="By",
    )
    metric_rows_scored = meter.create_counter(
        name="bqml.inference.rows_affected",
        description="Number of prediction rows appended to destination table",
        unit="1",
    )
    metric_eval_roc_auc = meter.create_gauge(
        name="bqml.evaluation.roc_auc",
        description="Area Under ROC Curve from continuous ML.EVALUATE",
        unit="1",
    )

    return tracer_provider, meter_provider


def record_job_stats(query_job, config, phase: str) -> Dict[str, int]:
    """Captures BigQuery job statistics onto the active span and metrics.

    Shared by every pipeline phase so FinOps attributes are recorded
    identically regardless of which stage issued the query.

    Args:
        query_job: A COMPLETED ``google.cloud.bigquery.QueryJob``.
        config: The active ``PipelineConfig``.
        phase: Short label distinguishing this job within the span,
            e.g. ``"ingest"``, ``"delete"`` or ``"insert"``.

    Returns:
        A dict with ``slot_millis``, ``bytes_billed`` and ``rows_affected``.
    """
    slot_millis = query_job.slot_millis or 0
    bytes_billed = query_job.total_bytes_billed or 0
    rows_affected = query_job.num_dml_affected_rows or 0

    span = trace.get_current_span()
    span.set_attribute(f"bq.{phase}.job_id", query_job.job_id)
    span.set_attribute(f"bq.{phase}.slot_millis", slot_millis)
    span.set_attribute(f"bq.{phase}.total_bytes_billed", bytes_billed)
    span.set_attribute(f"bq.{phase}.rows_affected", rows_affected)

    # OpenTelemetry database semantic conventions, so these traces stay legible
    # to any OTel-aware backend rather than only Cloud Trace.
    span.set_attribute("db.system", "bigquery")
    span.set_attribute("db.namespace", f"{config.gcp_project_id}.{config.bq_dataset_id}")

    attrs = {"model_name": config.model_name, "phase": phase}
    if metric_slot_millis is not None:
        metric_slot_millis.add(slot_millis, attributes=attrs)
    if metric_bytes_billed is not None:
        metric_bytes_billed.add(bytes_billed, attributes=attrs)

    return {
        "slot_millis": slot_millis,
        "bytes_billed": bytes_billed,
        "rows_affected": rows_affected,
    }
