"""Continuous model evaluation against realized production labels."""

import logging
from typing import Dict, Optional

from google.cloud import bigquery
from opentelemetry import trace

import src.telemetry as telemetry
from src.config import PipelineConfig

logger = logging.getLogger("bqml_orchestrator.evaluate")
tracer = trace.get_tracer(__name__)


def run_model_evaluation(
    client: bigquery.Client, config: PipelineConfig
) -> Optional[Dict[str, float]]:
    """Evaluates the model against matured labels in the evaluation partition.

    Unlike evaluation against a frozen holdout table -- which yields an identical
    metric on every run and can therefore never alert -- this measures the model
    against fresh production outcomes, making ROC-AUC a real decay signal.

    Returns:
        A dict of evaluation metrics, or ``None`` if no labeled rows were found.
    """
    with tracer.start_as_current_span("evaluate.labeled_production_check") as span:
        span.set_attribute("eval.model_name", config.model_name)
        span.set_attribute("eval.date", config.resolved_eval_date)
        span.set_attribute("eval.label_lag_days", config.eval_label_lag_days)
        span.set_attribute("eval.min_roc_auc", config.min_holdout_roc_auc)

        logger.info(
            f"Evaluating model '{config.model_name}' against realized labels "
            f"for {config.resolved_eval_date} "
            f"(label lag: {config.eval_label_lag_days}d)..."
        )

        rows = list(client.query(config.load_sql("evaluate_model.sql")).result())

        # ML.EVALUATE returns a single row of NULLs when the input has no
        # labeled records -- treat that as "not evaluable", not as a failure.
        if not rows or rows[0].roc_auc is None:
            msg = (
                f"No matured labels found for {config.resolved_eval_date}; "
                "skipping continuous evaluation. If labels in your domain lag, "
                "raise EVAL_LABEL_LAG_DAYS."
            )
            logger.warning(msg)
            span.add_event("evaluation_skipped", {"reason": msg})
            return None

        row = rows[0]
        metrics = {
            "roc_auc": float(row.roc_auc or 0.0),
            "log_loss": float(row.log_loss or 0.0),
            "accuracy": float(row.accuracy or 0.0),
            "precision": float(row.precision or 0.0),
            "recall": float(row.recall or 0.0),
            "f1_score": float(row.f1_score or 0.0),
        }

        for name, value in metrics.items():
            span.set_attribute(f"eval.{name}", value)

        if telemetry.metric_eval_roc_auc is not None:
            telemetry.metric_eval_roc_auc.set(
                metrics["roc_auc"], attributes={"model_name": config.model_name}
            )

        logger.info(
            f"Evaluation complete: ROC-AUC={metrics['roc_auc']:.4f}, "
            f"Log-Loss={metrics['log_loss']:.4f}, "
            f"Accuracy={metrics['accuracy']:.4f}"
        )

        # Warn rather than halt: a model-quality dip is a signal for retraining,
        # not a reason to withhold today's predictions. Drift (bad *input* data)
        # is the condition that justifies halting.
        if metrics["roc_auc"] < config.min_holdout_roc_auc:
            msg = (
                f"Model performance degradation: ROC-AUC is "
                f"{metrics['roc_auc']:.4f}, below the minimum of "
                f"{config.min_holdout_roc_auc}. Consider retraining."
            )
            logger.warning(msg)
            span.add_event("model_performance_warning", {"warning": msg})

        return metrics
