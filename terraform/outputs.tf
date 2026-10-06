output "cloud_run_job_name" {
  value       = google_cloud_run_v2_job.inference_job.name
  description = "The name of the provisioned Cloud Run Job."
}

output "cloud_run_job_id" {
  value       = google_cloud_run_v2_job.inference_job.id
  description = "Resource ID of the Cloud Run Job."
}

output "scheduler_job_name" {
  value       = google_cloud_scheduler_job.batch_trigger.name
  description = "The name of the Cloud Scheduler trigger job."
}

output "runner_service_account_email" {
  value       = google_service_account.runner_sa.email
  description = "Email of the IAM service account used by the Cloud Run worker."
}

output "scheduler_service_account_email" {
  value       = google_service_account.scheduler_sa.email
  description = "Email of the IAM service account used by Cloud Scheduler."
}

output "psi_drift_alert_policy_id" {
  value       = one(google_monitoring_alert_policy.psi_drift_alert[*].id)
  description = "Resource ID of the statistical drift alert policy (null while enable_alert_policies = false)."
}
