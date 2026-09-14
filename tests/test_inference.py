"""Unit tests for idempotent batch inference."""

from unittest.mock import MagicMock

from src.config import PipelineConfig
from tests.helpers import strip_sql_comments
from src.inference import run_batch_inference


def make_job(job_id, slot_millis, bytes_billed, rows):
    job = MagicMock()
    job.job_id = job_id
    job.slot_millis = slot_millis
    job.total_bytes_billed = bytes_billed
    job.num_dml_affected_rows = rows
    job.result.return_value = []
    return job


def test_runs_delete_and_insert_as_separate_jobs():
    """Two single-statement jobs, never one script job.

    A combined multi-statement script would null out num_dml_affected_rows,
    which is the regression this test guards against.
    """
    config = PipelineConfig(
        gcp_project_id="test-proj", target_date="2022-02-10", enable_cloud_exporters=False
    )
    delete_job = make_job("job_del", 100, 1000, 0)
    insert_job = make_job("job_ins", 5000, 250000, 24817)

    client = MagicMock()
    client.query.side_effect = [delete_job, insert_job]

    result = run_batch_inference(client, config)

    assert client.query.call_count == 2, "DELETE and INSERT must be separate jobs"

    delete_sql = strip_sql_comments(client.query.call_args_list[0].args[0])
    insert_sql = strip_sql_comments(client.query.call_args_list[1].args[0])

    assert delete_sql.upper().startswith("DELETE")
    assert insert_sql.upper().startswith("INSERT")
    assert "ML.PREDICT" in insert_sql

    # Each must be a SINGLE statement. More than one trailing semicolon means
    # BigQuery would compile a script job and null out num_dml_affected_rows.
    assert delete_sql.rstrip(";").count(";") == 0, "DELETE must be single-statement"
    assert insert_sql.rstrip(";").count(";") == 0, "INSERT must be single-statement"

    # The join key must survive into the executed SQL.
    assert "trip_id" in insert_sql

    assert result["rows_scored"] == 24817
    assert result["slot_millis"] == 5100
    assert result["bytes_billed"] == 251000


def test_backfill_reports_replaced_rows():
    """Re-running an already-scored partition must clear before inserting."""
    config = PipelineConfig(
        gcp_project_id="test-proj", target_date="2022-02-10", enable_cloud_exporters=False
    )
    client = MagicMock()
    client.query.side_effect = [
        make_job("job_del", 80, 900, 24817),   # prior run's rows removed
        make_job("job_ins", 5200, 250000, 24817),
    ]

    result = run_batch_inference(client, config)

    assert result["rows_deleted"] == 24817
    assert result["rows_scored"] == 24817


def test_handles_none_job_statistics():
    """BigQuery returns None for stats in some job shapes; must not crash."""
    config = PipelineConfig(
        gcp_project_id="test-proj", target_date="2022-02-10", enable_cloud_exporters=False
    )
    client = MagicMock()
    client.query.side_effect = [
        make_job("job_del", None, None, None),
        make_job("job_ins", None, None, None),
    ]

    result = run_batch_inference(client, config)
    assert result == {
        "rows_scored": 0,
        "rows_deleted": 0,
        "slot_millis": 0,
        "bytes_billed": 0,
    }
