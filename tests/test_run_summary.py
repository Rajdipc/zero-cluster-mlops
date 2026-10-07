"""Unit tests for the per-run status message that the run-status email carries.

The message is the first thing an on-call engineer reads, so each status must
say what happened and what to do next, in plain words.
"""

from src.drift import DriftDetectedException
from src.run_summary import (
    ALERT_FIELDS,
    RUN_SUMMARY_EVENT,
    STATUS_FAILED,
    STATUS_HALTED,
    STATUS_SUCCEEDED,
    RunSummary,
)


def _summary(**kwargs) -> RunSummary:
    base = {"target_date": "2026-10-06", "psi_threshold": 0.25}
    base.update(kwargs)
    return RunSummary(**base)


def test_success_message_reports_rows_date_drift_and_auc():
    fields = _summary(psi=0.0123, roc_auc=0.7906, rows_scored=69817).build(0, 252.0)
    assert fields["status"] == STATUS_SUCCEEDED
    assert fields["status_message"] == (
        "SUCCEEDED: scored 69,817 rows for 2026-10-06 in 4m 12s. "
        "Drift PSI 0.0123 (threshold 0.25). ROC-AUC 0.7906."
    )


def test_success_without_labels_says_auc_was_not_evaluated():
    fields = _summary(psi=0.01, rows_scored=10).build(0, 9.0)
    assert "ROC-AUC not evaluated" in fields["status_message"]


def test_guardrail_halt_says_nothing_was_written_and_not_to_retry():
    exc = DriftDetectedException("PSI is 0.4500 (Threshold: 0.25)")
    fields = _summary(psi=0.45).build(2, 30.0, exc)
    assert fields["status"] == STATUS_HALTED
    assert fields["exit_code"] == "2"
    assert "before any predictions were written" in fields["status_message"]
    assert "PSI is 0.4500" in fields["status_message"]
    assert "Do not retry" in fields["status_message"]


def test_unexpected_failure_names_the_error_and_says_retry_is_safe():
    fields = _summary().build(3, 61.0, RuntimeError("503 backend unavailable"))
    assert fields["status"] == STATUS_FAILED
    assert "RuntimeError: 503 backend unavailable" in fields["status_message"]
    assert "re-running TARGET_DATE=2026-10-06 is safe" in fields["status_message"]


def test_every_field_the_alert_extracts_is_emitted_as_a_string():
    fields = _summary(rows_scored=1).build(0, 1.0)
    assert fields["event"] == RUN_SUMMARY_EVENT
    for name in ALERT_FIELDS:
        assert isinstance(fields[name], str) and fields[name], name


def test_execution_name_comes_from_the_cloud_run_environment(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "bqml-taxi-batch-worker-abc12")
    assert _summary().build(0, 1.0)["execution"] == "bqml-taxi-batch-worker-abc12"
    monkeypatch.delenv("CLOUD_RUN_EXECUTION")
    assert _summary().build(0, 1.0)["execution"] == "local"
