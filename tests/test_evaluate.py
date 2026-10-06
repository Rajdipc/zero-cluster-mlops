"""Unit tests for continuous evaluation against realized production labels.

WHY THIS FILE EXISTS
--------------------
Evaluation had no tests, even though the post markets it as "continuous
evaluation that actually detects something". Two behaviours here are easy to
break and silent when broken:

  1. WARN, DO NOT HALT. A model-quality dip is a retraining signal, not a
     reason to withhold today's predictions. Drift -- bad INPUT data -- is the
     condition that justifies halting. If evaluation ever starts raising, a
     mildly degraded model takes the entire pipeline down.

  2. NO LABELS IS NOT A FAILURE. ML.EVALUATE returns a single row of NULLs when
     nothing in the partition is labeled yet. Treating that row as real metrics
     would report ROC-AUC 0.0 and fire a spurious degradation warning on every
     run in any domain where labels lag.
"""

from collections import namedtuple
from unittest.mock import MagicMock

import pytest

from src.config import PipelineConfig
from src.evaluate import run_model_evaluation

EvalRow = namedtuple(
    "EvalRow", ["roc_auc", "log_loss", "accuracy", "precision", "recall", "f1_score"]
)


def make_row(roc_auc=0.77, **overrides) -> EvalRow:
    values = dict(
        roc_auc=roc_auc, log_loss=0.61, accuracy=0.68, precision=0.70, recall=0.65, f1_score=0.67
    )
    values.update(overrides)
    return EvalRow(**values)


def make_config(**overrides) -> PipelineConfig:
    defaults = dict(
        gcp_project_id="test-proj",
        target_date="2022-02-10",
        enable_cloud_exporters=False,
    )
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def mock_client(rows):
    job = MagicMock()
    job.result.return_value = rows
    client = MagicMock()
    client.query.return_value = job
    return client


# ==============================================================================
# The metric path
# ==============================================================================
def test_returns_metrics_for_a_labeled_partition():
    metrics = run_model_evaluation(mock_client([make_row(roc_auc=0.7134)]), make_config())

    assert metrics is not None
    assert metrics["roc_auc"] == pytest.approx(0.7134)
    assert set(metrics) == {"roc_auc", "log_loss", "accuracy", "precision", "recall", "f1_score"}


def test_honest_roc_auc_is_not_mistaken_for_degradation():
    """~0.77 is what this model scores once the leaked column is removed.

    Measured on the live deploy; the leaky variant scores ~0.81. The default floor sits below the honest value on
    purpose, so removing the leak does not immediately trip a warning.
    """
    config = make_config()
    assert 0.77 > config.min_holdout_roc_auc


# ==============================================================================
# Warn, do not halt
# ==============================================================================
def test_degraded_model_warns_but_still_returns():
    """Must NOT raise: today's predictions are still worth producing."""
    metrics = run_model_evaluation(
        mock_client([make_row(roc_auc=0.41)]), make_config(min_holdout_roc_auc=0.60)
    )

    assert metrics is not None
    assert metrics["roc_auc"] == pytest.approx(0.41)


def test_degradation_is_logged_as_a_warning(caplog):
    with caplog.at_level("WARNING"):
        run_model_evaluation(
            mock_client([make_row(roc_auc=0.41)]), make_config(min_holdout_roc_auc=0.60)
        )
    assert any("degradation" in record.message.lower() for record in caplog.records)


def test_exactly_at_the_floor_does_not_warn(caplog):
    """The comparison is strict (<), so the boundary value is acceptable."""
    with caplog.at_level("WARNING"):
        run_model_evaluation(
            mock_client([make_row(roc_auc=0.60)]), make_config(min_holdout_roc_auc=0.60)
        )
    assert not any("degradation" in record.message.lower() for record in caplog.records)


# ==============================================================================
# Unlabeled partitions
# ==============================================================================
def test_all_null_row_is_treated_as_not_evaluable():
    """ML.EVALUATE returns one row of NULLs when no labels exist yet."""
    null_row = make_row(roc_auc=None, log_loss=None, accuracy=None)
    assert run_model_evaluation(mock_client([null_row]), make_config()) is None


def test_empty_result_is_treated_as_not_evaluable():
    assert run_model_evaluation(mock_client([]), make_config()) is None


def test_unlabeled_partition_does_not_warn_about_degradation(caplog):
    """Otherwise every run in a lagging-label domain reports a fake ROC-AUC of 0."""
    with caplog.at_level("WARNING"):
        run_model_evaluation(mock_client([make_row(roc_auc=None)]), make_config())

    assert not any("degradation" in record.message.lower() for record in caplog.records)
    assert any("matured labels" in record.message for record in caplog.records)


# ==============================================================================
# Which partition gets evaluated
# ==============================================================================
def test_evaluates_the_lag_adjusted_partition():
    """With a label lag, evaluation must target an older, matured partition."""
    client = mock_client([make_row()])
    config = make_config(target_date="2022-02-10", eval_label_lag_days=7)

    run_model_evaluation(client, config)

    sql = client.query.call_args[0][0]
    assert "DATE('2022-02-03')" in sql
    assert "DATE('2022-02-10')" not in sql, "evaluated the unmatured target partition"


def test_evaluates_the_target_partition_when_labels_are_immediate():
    """Taxi tips are known at trip completion, so lag 0 is correct here."""
    client = mock_client([make_row()])

    run_model_evaluation(client, make_config(target_date="2022-02-10", eval_label_lag_days=0))

    assert "DATE('2022-02-10')" in client.query.call_args[0][0]


def test_issues_exactly_one_query():
    """Evaluation is a read; it must not turn into a multi-statement script."""
    client = mock_client([make_row()])
    run_model_evaluation(client, make_config())
    assert client.query.call_count == 1
