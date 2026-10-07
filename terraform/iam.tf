# ==============================================================================
# IAM: genuinely least-privilege execution and invocation identities
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. Cloud Run Job runner identity
# ------------------------------------------------------------------------------
resource "google_service_account" "runner_sa" {
  account_id   = "sa-bqml-batch-runner"
  display_name = "BQML Batch Worker Runner"
  description  = "Identity used by the Cloud Run Job task to query BigQuery and emit telemetry."
}

# Project-level roles.
#
# Only roles that genuinely cannot be scoped narrower belong here:
#   - bigquery.jobUser is project-scoped by definition; creating a query job is
#     a project-level operation and has no dataset-level equivalent.
#   - The telemetry writer roles target project-level ingestion endpoints.
#
# Note that bigquery.dataEditor is deliberately NOT in this list. Granting it at
# project scope would let the runner read and write EVERY dataset in the
# project, which is the opposite of least privilege. It is bound at dataset
# scope below instead.
locals {
  runner_project_roles = [
    "roles/bigquery.jobUser",
    "roles/cloudtrace.agent",
    "roles/monitoring.metricWriter",
    "roles/logging.logWriter",
  ]
}

resource "google_project_iam_member" "runner_project_roles" {
  for_each = toset(local.runner_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.runner_sa.email}"
}

# Dataset-scoped data access: read and write confined to this one dataset.
resource "google_bigquery_dataset_iam_member" "runner_dataset_editor" {
  project    = var.project_id
  dataset_id = var.bq_dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runner_sa.email}"
}

# ------------------------------------------------------------------------------
# 2. Cloud Scheduler invoker identity
# ------------------------------------------------------------------------------
resource "google_service_account" "scheduler_sa" {
  account_id   = "sa-bqml-scheduler-invoker"
  display_name = "Cloud Scheduler Batch Invoker"
  description  = "Used by Cloud Scheduler to trigger the Cloud Run Job (OAuth token)."
}

# Invoker rights on this specific job only, not project-wide run.invoker.
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.inference_job.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}
