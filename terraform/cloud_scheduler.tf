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

    # OAuth, not OIDC. This URI is a Google API (*.googleapis.com), and Google
    # APIs only accept OAuth access tokens. An oidc_token here is rejected with
    # 401 UNAUTHENTICATED at every scheduled attempt, so the job silently never
    # runs. OIDC is for calling your own Cloud Run services or functions.
    oauth_token {
      service_account_email = google_service_account.scheduler_sa.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_cloud_run_v2_job.inference_job,
    google_cloud_run_v2_job_iam_member.scheduler_invoker
  ]
}
