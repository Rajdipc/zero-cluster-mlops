"""Unit tests for the drift guardrail and circuit breaker."""

from collections import namedtuple
from unittest.mock import MagicMock

import pytest

from src.config import PipelineConfig
from src.drift import (
    DriftDetectedException,
    InsufficientDataException,
    run_drift_guardrail,
)

CountRow = namedtuple("CountRow", ["row_count"])
PsiRow = namedtuple("PsiRow", ["total_psi"])


def make_config(**overrides) -> PipelineConfig:
    defaults = dict(
        gcp_project_id="test-proj",
        target_date="2022-02-10",
        psi_drift_threshold=0.25,
        enable_cloud_exporters=False,
    )
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def mock_client(*result_sets):
    """Builds a BigQuery client whose successive query() calls return the given rows."""
    jobs = []
    for rows in result_sets:
        job = MagicMock()
        job.result.return_value = rows
        jobs.append(job)
    client = MagicMock()
    client.query.side_effect = jobs
    return client


def test_halts_on_empty_partition():
    client = mock_client([CountRow(row_count=0)])
    with pytest.raises(InsufficientDataException, match="below the minimum"):
        run_drift_guardrail(client, make_config())


def test_halts_on_suspiciously_low_volume():
    """A partially loaded partition must not slip through a bare zero-row check."""
    client = mock_client([CountRow(row_count=12)])
    config = make_config(min_row_count=1000)
    with pytest.raises(InsufficientDataException):
        run_drift_guardrail(client, config)


def test_trips_circuit_breaker_on_high_psi():
    client = mock_client([CountRow(row_count=5000)], [PsiRow(total_psi=0.3412)])
    with pytest.raises(DriftDetectedException, match="0.3412"):
        run_drift_guardrail(client, make_config())


def test_trips_exactly_at_threshold():
    """The threshold is inclusive; PSI == threshold must halt."""
    client = mock_client([CountRow(row_count=5000)], [PsiRow(total_psi=0.25)])
    with pytest.raises(DriftDetectedException):
        run_drift_guardrail(client, make_config())


def test_passes_on_low_psi():
    client = mock_client([CountRow(row_count=5000)], [PsiRow(total_psi=0.0412)])
    assert run_drift_guardrail(client, make_config()) == pytest.approx(0.0412)


def test_null_psi_treated_as_zero():
    """APPROX_QUANTILES can yield NULL on degenerate input; must not crash."""
    client = mock_client([CountRow(row_count=5000)], [PsiRow(total_psi=None)])
    assert run_drift_guardrail(client, make_config()) == 0.0
