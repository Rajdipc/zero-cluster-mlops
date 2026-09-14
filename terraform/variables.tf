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
