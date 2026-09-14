variable "project_id" {
  type        = string
  description = "The target Google Cloud project ID."
}

variable "region" {
  type        = string
  description = "Region for Cloud Run and Cloud Scheduler. Distinct from the BigQuery location."
  default     = "us-central1"
}

variable "bq_location" {
  type        = string
  description = <<-EOT
    BigQuery dataset location. Must be "US" for this blueprint, because the
    seed step joins against bigquery-public-data, which lives in the US
    multi-region. BigQuery cannot query across locations.
  EOT
  default     = "US"
}

variable "bq_dataset_id" {
  type        = string
  description = "The BigQuery dataset ID holding the feature table, model and predictions."
  default     = "ml_production"
}

variable "container_image" {
  type        = string
  description = "Fully-qualified container image URI in Artifact Registry."
}

variable "notification_email" {
  type        = string
  description = "Email address for Cloud Monitoring alert notifications."
}

variable "schedule_cron" {
  type        = string
  description = "Cron schedule for the batch scoring trigger (UTC)."
  default     = "0 2 * * *"
}

variable "psi_drift_threshold" {
  type        = number
  description = <<-EOT
    Population Stability Index threshold above which the circuit breaker trips.
    Convention: <0.10 no meaningful shift, 0.10-0.25 moderate, >0.25 significant.
    Typed as a number so it can be used directly in the alert policy threshold.
  EOT
  default     = 0.25
}

variable "max_slot_millis_warning" {
  type        = number
  description = "Slot-milliseconds per run above which a cost warning is raised."
  default     = 300000
}

variable "min_row_count" {
  type        = number
  description = <<-EOT
    Minimum row count the target partition must contain before scoring proceeds.

    The default of 1 is deliberately a bare presence check: it catches a
    partition that is entirely missing, which is the failure this blueprint can
    detect without knowing your data. It does NOT catch a partially loaded
    partition.

    To catch partial loads, raise this to roughly half of your typical daily
    volume once you know what typical looks like. Query the feature table for
    the p50 row count per partition over the last 30 days and halve it. A
    partition below that is far more likely to be a broken upstream job than a
    genuinely quiet day.
  EOT
  default     = 1
}

variable "enable_demo_ingestion" {
  type        = bool
  description = <<-EOT
    Whether the job synthesizes its own input partition from the public dataset
    when one is absent (Phase 0).

    TRUE keeps the public-dataset demo self-sustaining: the seed script plants
    only a single "yesterday" partition, so without this the scheduled job
    correctly fails every subsequent night with InsufficientDataException.

    SET FALSE FOR ANY REAL DEPLOYMENT. Populating the feature table is the
    upstream ETL's responsibility. A scoring pipeline that also ingests its own
    input cannot distinguish "upstream is late" from "upstream is broken", and
    demo ingestion additionally scans the public dataset on every run.
  EOT
  default     = true
}

variable "job_name" {
  type        = string
  description = "Name of the Cloud Run Job."
  default     = "bqml-taxi-batch-worker"
}

variable "model_name" {
  type        = string
  description = "Name of the BigQuery ML model."
  default     = "taxi_tip_model"
}

variable "demo_source_table" {
  type        = string
  default     = "bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022"
  description = <<-EOT
    Public BigQuery table backing demo ingestion. The NYC TLC dataset stores one
    table per year (tlc_yellow_trips_YYYY); 2011-2022 are populated and share an
    identical 20-column schema. The 2023 table exists but is empty.

    The year here must match the year in demo_source_window_start; the container
    validates this at startup and refuses to run on a mismatch.

    Ignored entirely when enable_demo_ingestion is false.
  EOT
}

variable "demo_source_window_start" {
  type        = string
  default     = "2022-02-01"
  description = <<-EOT
    First day of the historical window that demo ingestion cycles through.

    This is one half of a coupled pair: its year MUST match the year in
    demo_source_table. The container enforces that at startup and exits with a
    validation error rather than silently ingesting zero rows.

    Both halves are declared here precisely so that changing the demo year is a
    single coherent edit. Exposing only the table -- as an earlier revision of
    this file did -- makes the container's own error message unactionable,
    because it names an environment variable the operator has no way to set.

    Ignored entirely when enable_demo_ingestion is false.
  EOT
}

variable "demo_source_window_days" {
  type        = number
  default     = 28
  description = <<-EOT
    Length in days of the demo source window. The target date is mapped onto the
    window modulo this value, so the demo keeps producing a plausible partition
    indefinitely instead of running out of source data after one night.

    28 rather than 30 or 31 so the mapping preserves day-of-week alignment:
    taxi volume has a strong weekly cycle, and a 30-day rotation would drift
    weekday data onto weekend dates and manufacture drift that is an artifact of
    the demo rather than a property of the data.

    Ignored entirely when enable_demo_ingestion is false.
  EOT
}
