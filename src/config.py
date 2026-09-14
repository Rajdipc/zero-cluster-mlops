"""Pipeline configuration schema using Pydantic v2."""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PipelineConfig(BaseSettings):
    """Configuration settings for the BQML serverless batch inference pipeline."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )

    # --- Google Cloud ---------------------------------------------------------
    gcp_project_id: str = Field(
        default_factory=lambda: os.environ.get("GCP_PROJECT_ID", "local-dev-project"),
        description="Target Google Cloud Project ID.",
    )
    gcp_region: str = Field(
        default="us-central1",
        description="Region for Cloud Run. Distinct from the BigQuery location.",
    )
    bq_location: str = Field(
        default="US",
        description=(
            "BigQuery dataset location. Must be 'US' to join against "
            "bigquery-public-data, which lives in the US multi-region. "
            "BigQuery cannot query across locations."
        ),
    )

    # --- BigQuery objects -----------------------------------------------------
    bq_dataset_id: str = Field(default="ml_production")
    model_name: str = Field(default="taxi_tip_model")
    features_table: str = Field(
        default="taxi_trips_features",
        description="Unified partitioned feature table (training, baseline and scoring).",
    )
    feature_view: str = Field(
        default="v_taxi_features",
        description="Canonical feature contract. Single source of truth against skew.",
    )
    predictions_table: str = Field(
        default="taxi_predictions",
        description="Destination table, partitioned by scoring_date.",
    )

    # --- Windows --------------------------------------------------------------
    target_date: Optional[str] = Field(
        default=None,
        description="Target partition date (YYYY-MM-DD). Defaults to yesterday (UTC).",
    )
    baseline_start_date: str = Field(
        default="2022-01-01",
        description="Inclusive start of the PSI reference window.",
    )
    baseline_end_date: str = Field(
        default="2022-01-15",
        description="Inclusive end of the PSI reference window.",
    )
    eval_label_lag_days: int = Field(
        default=0,
        ge=0,
        description=(
            "How many days labels take to mature. 0 for taxi tips (known at trip "
            "completion). Raise this for domains like churn or chargebacks so "
            "evaluation targets a partition whose labels actually exist."
        ),
    )

    # --- Guardrails -----------------------------------------------------------
    psi_drift_threshold: float = Field(
        default=0.25,
        description="PSI above which the circuit breaker trips. <0.1 none, 0.1-0.25 moderate, >0.25 significant.",
    )
    canary_feature: str = Field(
        default="fare_amount",
        description="Numerical feature monitored for distribution drift.",
    )
    min_holdout_roc_auc: float = Field(
        default=0.60,
        description="ROC-AUC below which a model-degradation warning is emitted.",
    )
    min_row_count: int = Field(
        default=1,
        ge=0,
        description=(
            "Minimum acceptable row count in the target partition. A bare "
            "zero-row check only catches total absence; a partition at 10% of "
            "normal volume indicates a partially failed upstream job."
        ),
    )

    # --- Demo ingestion (Phase 0) --------------------------------------------
    #
    # DEMO ONLY. Real deployments set enable_demo_ingestion=false and let their
    # upstream ETL populate the feature table. See src/ingest.py.
    enable_demo_ingestion: bool = Field(
        default=True,
        description=(
            "Synthesize the target feature partition from the public dataset "
            "when it is absent. Keeps the public-dataset demo self-sustaining "
            "past day one. SET FALSE IN ANY REAL DEPLOYMENT -- ingestion is the "
            "upstream ETL's responsibility, not the scoring pipeline's."
        ),
    )
    demo_source_table: str = Field(
        default="bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022",
        description=(
            "Public source table for demo ingestion. The dataset stores one "
            "table per year, tlc_yellow_trips_YYYY. Verified populated for "
            "2011-2022 with an identical 20-column schema. NOTE: the 2023 table "
            "EXISTS but is EMPTY and has only 19 columns (no airport_fee), so "
            "pointing at it yields zero rows and a confusing downstream halt."
        ),
    )
    demo_source_window_start: str = Field(
        default="2022-02-01",
        description="First day of the historical window that demo ingestion cycles through.",
    )
    demo_source_window_days: int = Field(
        default=28,
        ge=1,
        description="Length of the demo source window. The target date maps onto it modulo this.",
    )

    @model_validator(mode="after")
    def _demo_window_must_match_source_year(self) -> "PipelineConfig":
        """Fail fast when the source table year and the window year disagree.

        Without this guard a mismatch inserts zero rows, and the failure only
        surfaces two phases later as `InsufficientDataException: partition has
        0 rows`. That message points at the guardrail, not at the typo that
        actually caused it, and costs you an afternoon.
        """
        if not self.enable_demo_ingestion:
            return self

        table_year = re.search(r"(\d{4})\s*$", self.demo_source_table)
        window_year = re.match(r"(\d{4})-", self.demo_source_window_start)
        if not table_year or not window_year:
            return self  # non-standard naming; nothing reliable to compare

        if table_year.group(1) != window_year.group(1):
            raise ValueError(
                f"Demo ingestion misconfigured: DEMO_SOURCE_TABLE points at year "
                f"{table_year.group(1)} ('{self.demo_source_table}') but "
                f"DEMO_SOURCE_WINDOW_START is in {window_year.group(1)} "
                f"('{self.demo_source_window_start}'). That window selects zero rows. "
                f"Set both to the same year, or set ENABLE_DEMO_INGESTION=false."
            )
        return self

    # --- Observability --------------------------------------------------------
    enable_cloud_exporters: bool = Field(default=True)
    metric_export_interval_millis: int = Field(
        default=60000,
        ge=10000,
        description=(
            "Cloud Monitoring rejects two points for the same time series inside "
            "5 seconds. A short interval plus the shutdown force-flush collides "
            "and silently drops metrics. Keep this well above 10s and rely on "
            "the shutdown flush as the real export for short-lived jobs."
        ),
    )

    sql_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "sql",
        description="Directory containing the SQL templates.",
    )

    # --- Derived --------------------------------------------------------------
    @property
    def resolved_target_date(self) -> str:
        """The partition to score. Defaults to yesterday (UTC)."""
        if self.target_date and self.target_date.strip():
            return self.target_date.strip()
        return (datetime.now(timezone.utc).date() - timedelta(days=1)).strftime("%Y-%m-%d")

    @property
    def target_date_sql(self) -> str:
        """SQL literal for the scoring partition date."""
        return f"DATE('{self.resolved_target_date}')"

    @property
    def resolved_eval_date(self) -> str:
        """The partition to evaluate, backed off by the label maturation lag."""
        target = datetime.strptime(self.resolved_target_date, "%Y-%m-%d").date()
        return (target - timedelta(days=self.eval_label_lag_days)).strftime("%Y-%m-%d")

    @property
    def eval_date_sql(self) -> str:
        """SQL literal for the evaluation partition date."""
        return f"DATE('{self.resolved_eval_date}')"

    def load_sql(self, filename: str) -> str:
        """Reads a SQL template and substitutes configuration placeholders."""
        path = self.sql_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing SQL template: {path}")

        return path.read_text(encoding="utf-8").format(
            project_id=self.gcp_project_id,
            dataset_id=self.bq_dataset_id,
            model_name=self.model_name,
            features_table=self.features_table,
            feature_view=self.feature_view,
            predictions_table=self.predictions_table,
            feature_name=self.canary_feature,
            baseline_start_date=self.baseline_start_date,
            baseline_end_date=self.baseline_end_date,
            demo_source_table=self.demo_source_table,
            demo_source_window_start=self.demo_source_window_start,
            demo_source_window_days=self.demo_source_window_days,
            target_date_sql=self.target_date_sql,
            eval_date_sql=self.eval_date_sql,
        )
