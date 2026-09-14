# ==============================================================================
# Cloud Scheduler: Scheduled Daily Trigger for Cloud Run Job
# ==============================================================================

resource "google_cloud_scheduler_job" "batch_trigger" {
  name        = "trigger-bqml-taxi-batch-scoring"
  description = "Triggers daily BQML batch push-down inference via Cloud Run Jobs API"
  schedule    = var.schedule_cron
  time_zone   = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.inference_job.name}:run"

    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = "https://${var.region}-run.googleapis.com/"
    }
  }

  depends_on = [
    google_cloud_run_v2_job.inference_job,
    google_cloud_run_v2_job_iam_member.scheduler_invoker
  ]
}
