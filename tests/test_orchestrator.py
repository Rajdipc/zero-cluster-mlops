"""Unit tests for the orchestrator's exit-code contract and shutdown guarantees.

WHY THIS FILE EXISTS
--------------------
The orchestrator had no tests, despite owning the two behaviours the whole
design rests on:

  1. THE EXIT-CODE CONTRACT. Cloud Run is configured with max_retries = 0
     precisely because a guardrail halt is terminal -- retrying reads the same
     bad partition and fails identically. Exit 2 (terminal) and exit 3
     (possibly transient) are how an operator tells those apart. If the two
     ever collapse into one code, the retry policy documented in cloud_run.tf
     becomes unreasonable and nothing else would notice.

  2. THE HALT ACTUALLY HALTS. A drift detection that logs loudly and then
     writes predictions anyway is worse than no guardrail at all, because it
     looks like it worked. `test_halt_prevents_scoring` is the single most
     important assertion in this file.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.drift import DriftDetectedException, InsufficientDataException
from src.orchestrator import EXIT_GUARDRAIL_HALT, EXIT_OK, EXIT_UNEXPECTED, main


@pytest.fixture
def pipeline(monkeypatch):
    """Patches every boundary the orchestrator touches and yields the mocks.

    Nothing here reaches BigQuery, Cloud Trace or Cloud Monitoring: the point is
    to test control flow, not integration.
    """
    monkeypatch.setenv("GCP_PROJECT_ID", "test-proj")
    monkeypatch.setenv("ENABLE_CLOUD_EXPORTERS", "false")

    tracer_provider, meter_provider = MagicMock(), MagicMock()

    with patch("src.orchestrator.setup_telemetry", return_value=(tracer_provider, meter_provider)), \
         patch("src.orchestrator.bigquery.Client") as client, \
         patch("src.orchestrator.run_demo_ingestion") as ingest, \
         patch("src.orchestrator.run_drift_guardrail") as drift, \
         patch("src.orchestrator.run_model_evaluation") as evaluate, \
         patch("src.orchestrator.run_batch_inference") as inference:
        yield {
            "client": client,
            "ingest": ingest,
            "drift": drift,
            "evaluate": evaluate,
            "inference": inference,
            "tracer_provider": tracer_provider,
            "meter_provider": meter_provider,
        }


def run_main() -> int:
    """Runs main() and returns the exit code it terminated with."""
    with pytest.raises(SystemExit) as excinfo:
        main()
    return excinfo.value.code


# ==============================================================================
# Exit-code contract
# ==============================================================================
def test_happy_path_exits_zero_and_runs_every_phase(pipeline):
    assert run_main() == EXIT_OK

    for phase in ("ingest", "drift", "evaluate", "inference"):
        assert pipeline[phase].call_count == 1, f"phase {phase} did not run exactly once"


@pytest.mark.parametrize(
    "exception",
    [
        DriftDetectedException("PSI is 0.41"),
        InsufficientDataException("partition has 0 rows"),
    ],
)
def test_guardrail_failures_exit_two(pipeline, exception):
    """Terminal. Paired with max_retries = 0 so Cloud Run does not repeat the run."""
    pipeline["drift"].side_effect = exception
    assert run_main() == EXIT_GUARDRAIL_HALT


def test_unexpected_failures_exit_three(pipeline):
    """Possibly transient -- a BigQuery 5xx or quota blip. Safe to retry."""
    pipeline["drift"].side_effect = RuntimeError("503 Service Unavailable")
    assert run_main() == EXIT_UNEXPECTED


def test_terminal_and_transient_codes_are_distinguishable():
    """The whole retry policy rests on these three being different values.

    The literals are pinned, not just compared to each other: cloud_run.tf's
    max_retries comment and the README troubleshooting table both name them by
    number, so renumbering here would silently falsify the documentation.
    """
    assert EXIT_OK == 0, "a non-zero success code would make Cloud Run report failure"
    assert EXIT_GUARDRAIL_HALT == 2
    assert EXIT_UNEXPECTED == 3
    assert len({EXIT_OK, EXIT_GUARDRAIL_HALT, EXIT_UNEXPECTED}) == 3


# ==============================================================================
# The halt must actually halt
# ==============================================================================
def test_halt_prevents_scoring(pipeline):
    """A guardrail that detects drift and then scores anyway is worse than none."""
    pipeline["drift"].side_effect = DriftDetectedException("PSI is 0.41")

    assert run_main() == EXIT_GUARDRAIL_HALT
    pipeline["inference"].assert_not_called()
    pipeline["evaluate"].assert_not_called()


def test_ingestion_failure_stops_before_the_guardrail(pipeline):
    """Phase 0 precedes the guardrail, so its failure must not be scored through."""
    pipeline["ingest"].side_effect = RuntimeError("permission denied on public dataset")

    assert run_main() == EXIT_UNEXPECTED
    pipeline["drift"].assert_not_called()
    pipeline["inference"].assert_not_called()


def test_evaluation_failure_does_not_silently_skip_scoring(pipeline):
    """Evaluation warns rather than halts, but a genuine CRASH must still surface."""
    pipeline["evaluate"].side_effect = RuntimeError("ML.EVALUATE exploded")

    assert run_main() == EXIT_UNEXPECTED
    pipeline["inference"].assert_not_called()


# ==============================================================================
# Telemetry flush
# ==============================================================================
def test_telemetry_is_flushed_on_success(pipeline):
    """Cloud Run freezes vCPUs when main() returns; unflushed spans are lost."""
    run_main()
    pipeline["tracer_provider"].shutdown.assert_called_once()
    pipeline["meter_provider"].shutdown.assert_called_once()


def test_telemetry_is_flushed_on_failure(pipeline):
    """The failing run is the one whose trace you actually need."""
    pipeline["drift"].side_effect = DriftDetectedException("PSI is 0.41")

    run_main()
    pipeline["tracer_provider"].shutdown.assert_called_once()
    pipeline["meter_provider"].shutdown.assert_called_once()


def test_flush_failure_does_not_mask_the_exit_code(pipeline):
    """Losing telemetry is bad; reporting a drift halt as success is far worse."""
    pipeline["drift"].side_effect = DriftDetectedException("PSI is 0.41")
    pipeline["tracer_provider"].shutdown.side_effect = RuntimeError("exporter is gone")

    assert run_main() == EXIT_GUARDRAIL_HALT


def test_bigquery_client_uses_the_dataset_location_not_the_run_region(pipeline, monkeypatch):
    """BQ_LOCATION and GCP_REGION are deliberately separate; conflating them
    breaks the join against bigquery-public-data in the US multi-region."""
    monkeypatch.setenv("GCP_REGION", "europe-west1")
    monkeypatch.setenv("BQ_LOCATION", "US")

    run_main()

    _, kwargs = pipeline["client"].call_args
    assert kwargs["location"] == "US"
    assert kwargs["project"] == "test-proj"


# ==============================================================================
# Run summary: the line the run-status email is built from
# ==============================================================================
def _summaries(caplog):
    return [r for r in caplog.records
            if getattr(r, "json_fields", {}).get("event") == "pipeline_run_summary"]


def test_success_writes_exactly_one_run_summary(pipeline, caplog):
    caplog.set_level("INFO")  # setup_telemetry (which sets INFO) is mocked out
    pipeline["drift"].return_value = 0.0123
    pipeline["evaluate"].return_value = {"roc_auc": 0.79}
    pipeline["inference"].return_value = {"rows_scored": 69817}

    assert run_main() == EXIT_OK

    (record,) = _summaries(caplog)
    assert record.levelname == "INFO"
    assert record.json_fields["status"] == "SUCCEEDED"
    assert record.json_fields["rows_scored"] == 69817
    assert record.getMessage().startswith("SUCCEEDED: scored 69,817 rows")


def test_guardrail_halt_still_writes_a_run_summary(pipeline, caplog):
    pipeline["drift"].side_effect = DriftDetectedException("PSI 0.45")

    assert run_main() == EXIT_GUARDRAIL_HALT

    (record,) = _summaries(caplog)
    assert record.levelname == "ERROR"
    assert record.json_fields["status"] == "HALTED"
    assert record.json_fields["exit_code"] == "2"
