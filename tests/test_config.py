"""Unit tests for PipelineConfig."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.config import PipelineConfig
from tests.helpers import strip_sql_comments


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolates tests from the developer's real environment and .env file."""
    for var in (
        "GCP_PROJECT_ID",
        "TARGET_DATE",
        "PSI_DRIFT_THRESHOLD",
        "EVAL_LABEL_LAG_DAYS",
        "BQ_LOCATION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        PipelineConfig, "model_config", {**PipelineConfig.model_config, "env_file": None}
    )


def test_defaults():
    config = PipelineConfig()
    assert config.bq_dataset_id == "ml_production"
    assert config.model_name == "taxi_tip_model"
    assert config.psi_drift_threshold == 0.25
    assert config.canary_feature == "fare_amount"

    # BigQuery location must default to the US multi-region so the seed step can
    # join against bigquery-public-data.
    assert config.bq_location == "US"

    # Must stay above Cloud Monitoring's 5s per-series write limit.
    assert config.metric_export_interval_millis >= 10000


def test_target_date_defaults_to_yesterday_utc():
    config = PipelineConfig()
    expected = (datetime.now(timezone.utc).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert config.resolved_target_date == expected
    assert config.target_date_sql == f"DATE('{expected}')"


def test_explicit_target_date_used_for_backfill():
    config = PipelineConfig(target_date="2022-02-10")
    assert config.resolved_target_date == "2022-02-10"
    assert config.target_date_sql == "DATE('2022-02-10')"


def test_eval_date_tracks_target_when_labels_are_immediate():
    config = PipelineConfig(target_date="2022-02-10", eval_label_lag_days=0)
    assert config.resolved_eval_date == "2022-02-10"


def test_eval_date_backs_off_by_label_lag():
    """Domains with delayed labels must evaluate an older, matured partition."""
    config = PipelineConfig(target_date="2022-02-10", eval_label_lag_days=7)
    assert config.resolved_eval_date == "2022-02-03"
    assert config.eval_date_sql == "DATE('2022-02-03')"


def test_negative_label_lag_rejected():
    with pytest.raises(ValueError):
        PipelineConfig(eval_label_lag_days=-1)


def test_short_metric_interval_rejected():
    """Guards the Cloud Monitoring 5s write-limit mitigation against regression."""
    with pytest.raises(ValueError):
        PipelineConfig(metric_export_interval_millis=5000)


def test_load_sql_substitutes_all_placeholders():
    config = PipelineConfig(gcp_project_id="proj-x", target_date="2022-02-10")

    rendered = config.load_sql("batch_inference.sql")

    assert "{" not in rendered.replace("{{", "").replace("}}", ""), (
        "Unsubstituted placeholder remains in rendered SQL"
    )
    assert "proj-x" in rendered
    assert "DATE('2022-02-10')" in rendered
    # The join key must survive into the rendered statement.
    assert "trip_id" in rendered


def test_all_sql_templates_render():
    """Every template must render with the standard placeholder set."""
    config = PipelineConfig(gcp_project_id="proj-x", target_date="2022-02-10")
    for name in (
        "calculate_psi.sql",
        "evaluate_model.sql",
        "delete_partition.sql",
        "batch_inference.sql",
    ):
        rendered = config.load_sql(name)
        assert rendered.strip(), f"{name} rendered empty"


# ==============================================================================
# Demo source table configuration and the year-mismatch guard.
#
# These exist because the failure they prevent is genuinely nasty: a mismatched
# year inserts zero rows, and the error surfaces two phases later pointing at
# the drift guardrail instead of at the config typo that caused it.
# ==============================================================================
class TestDemoSourceTable:
    def test_defaults_to_a_populated_year(self, monkeypatch):
        """2022 is verified to hold 36.2M rows; 2023 exists but is empty."""
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        cfg = PipelineConfig()
        assert cfg.demo_source_table.endswith("tlc_yellow_trips_2022")
        assert cfg.demo_source_window_start.startswith("2022")

    def test_rejects_year_mismatch(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        with pytest.raises(ValidationError, match="Demo ingestion misconfigured"):
            PipelineConfig(demo_source_window_start="2021-02-01")

    def test_allows_consistent_year_change(self, monkeypatch):
        """2011-2022 share an identical schema, so switching years is supported."""
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        cfg = PipelineConfig(
            demo_source_table="bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2015",
            demo_source_window_start="2015-02-01",
        )
        assert cfg.demo_source_table.endswith("2015")

    def test_guard_is_skipped_when_demo_ingestion_is_off(self, monkeypatch):
        """In production the source table is irrelevant, so it must not block startup."""
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        cfg = PipelineConfig(
            enable_demo_ingestion=False, demo_source_window_start="1999-01-01"
        )
        assert cfg.enable_demo_ingestion is False

    def test_source_table_reaches_the_sql(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        cfg = PipelineConfig(
            demo_source_table="bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2019",
            demo_source_window_start="2019-02-01",
        )
        assert "tlc_yellow_trips_2019" in cfg.load_sql("ingest_demo_partition.sql")


# ==============================================================================
# Target-leakage firewall.
#
# total_amount = fare + extra + mta_tax + TIP + tolls + surcharge + airport_fee,
# and the label is `tip_amount > 2.00`. Feeding total_amount to the model hands
# it the answer: measured on 830,783 card-paid training trips, the single rule
# `(total_amount - fare_amount) > 5.5` reproduces the label 87.1% of the time.
#
# The feature view is the single place that enforces the exclusion, so these
# tests guard the contract rather than each consumer.
# ==============================================================================
class TestNoTargetLeakage:
    LEAKY = "total_amount"

    def _feature_block(self, sql: str) -> str:
        """The SELECT list only, with comments stripped."""
        body = strip_sql_comments(sql)
        return body[: body.lower().index("from")] if "from" in body.lower() else body

    def test_feature_view_excludes_total_amount(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        ddl = strip_sql_comments(PipelineConfig().load_sql("create_tables.sql"))
        view = ddl[ddl.index("CREATE OR REPLACE VIEW"):]
        select_list = view[: view.lower().index("from")]
        assert self.LEAKY not in select_list, "total_amount leaked into the feature view"

    @pytest.mark.parametrize(
        "template", ["batch_inference.sql", "evaluate_model.sql"]
    )
    def test_runtime_consumers_never_select_total_amount(self, monkeypatch, template):
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        sql = strip_sql_comments(PipelineConfig().load_sql(template))
        assert self.LEAKY not in sql, f"{template} selects the leaked column"

    def test_training_sql_never_selects_total_amount(self, monkeypatch):
        """train_model.sql is rendered by the seed script, so read it raw.

        It carries placeholders (train_start_date, train_end_date) that the
        runtime loader deliberately does not supply, because training is a
        seed-time concern rather than a per-run one.
        """
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        raw = (PipelineConfig().sql_dir / "train_model.sql").read_text()
        assert self.LEAKY not in strip_sql_comments(raw)

    def test_no_class_weighting(self, monkeypatch):
        """Classes are ~62/38. Weighting would cost calibration for no benefit."""
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        raw = (PipelineConfig().sql_dir / "train_model.sql").read_text()
        assert "auto_class_weights" not in strip_sql_comments(raw)

    def test_total_amount_survives_as_a_base_table_column(self, monkeypatch):
        """It is real data worth auditing -- excluded as a FEATURE, not deleted."""
        monkeypatch.setenv("GCP_PROJECT_ID", "p")
        ddl = PipelineConfig().load_sql("create_tables.sql")
        assert "total_amount FLOAT64" in ddl
