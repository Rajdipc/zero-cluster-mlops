"""Unit tests for Phase 0 demo ingestion."""

from unittest.mock import MagicMock

from src.config import PipelineConfig
from src.ingest import run_demo_ingestion


def make_job(job_id="job_ing", slot_millis=500, bytes_billed=5000, rows=0):
    job = MagicMock()
    job.job_id = job_id
    job.slot_millis = slot_millis
    job.total_bytes_billed = bytes_billed
    job.num_dml_affected_rows = rows
    job.result.return_value = []
    return job


def make_config(**overrides) -> PipelineConfig:
    defaults = dict(
        gcp_project_id="test-proj",
        target_date="2022-02-10",
        enable_cloud_exporters=False,
    )
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def test_disabled_is_a_no_op():
    """Real deployments disable Phase 0; it must not touch BigQuery at all."""
    client = MagicMock()
    result = run_demo_ingestion(client, make_config(enable_demo_ingestion=False))

    client.query.assert_not_called()
    assert result["rows_affected"] == 0


def test_enabled_issues_one_statement():
    client = MagicMock()
    client.query.return_value = make_job(rows=24817)

    result = run_demo_ingestion(client, make_config(enable_demo_ingestion=True))

    assert client.query.call_count == 1
    assert result["rows_affected"] == 24817


def test_skip_reported_when_partition_already_populated():
    """The NOT EXISTS guard makes a skip indistinguishable from a no-op insert."""
    client = MagicMock()
    client.query.return_value = make_job(rows=0)

    result = run_demo_ingestion(client, make_config(enable_demo_ingestion=True))
    assert result["rows_affected"] == 0


def test_sql_is_single_statement_and_guarded():
    """Must stay single-statement (Gotcha #4) and keep its idempotency guard."""
    config = make_config(enable_demo_ingestion=True)
    sql = config.load_sql("ingest_demo_partition.sql")

    executable = "\n".join(
        line.split("--", 1)[0].strip()
        for line in sql.splitlines()
        if line.split("--", 1)[0].strip()
    )

    assert executable.upper().startswith("INSERT")
    assert executable.rstrip(";").count(";") == 0, "must be a single statement"
    assert "NOT EXISTS" in executable.upper(), "idempotency guard removed"
    assert "QUALIFY" in executable.upper(), "trip_id de-duplication removed"


def test_source_day_mapping_is_deterministic():
    """Backfills must be reproducible: same target date -> same source day."""
    a = make_config(target_date="2022-02-10").load_sql("ingest_demo_partition.sql")
    b = make_config(target_date="2022-02-10").load_sql("ingest_demo_partition.sql")
    assert a == b

    c = make_config(target_date="2022-03-15").load_sql("ingest_demo_partition.sql")
    assert a != c, "different target dates must render different SQL"


def test_window_length_is_configurable():
    config = make_config(demo_source_window_days=7, demo_source_window_start="2022-02-01")
    sql = config.load_sql("ingest_demo_partition.sql")
    assert "7" in sql
    assert "2022-02-01" in sql
