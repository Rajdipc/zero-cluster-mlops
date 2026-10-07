# ==============================================================================
# Cloud Monitoring: notification channels and alert policies
# ==============================================================================

resource "google_monitoring_notification_channel" "email" {
  display_name = "MLOps Incident Notification Email"
  type         = "email"

  labels = {
    email_address = var.notification_email
  }
}

# ------------------------------------------------------------------------------
# Statistical feature distribution drift
#
# Both alert policies below watch custom metrics that only exist after the job
# has run once, and Cloud Monitoring rejects a policy on an unknown metric with
# a 404. On a fresh project, apply once with enable_alert_policies = false,
# execute the job, then apply again. See variables.tf.
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "psi_drift_alert" {
  count        = var.enable_alert_policies ? 1 : 0
  display_name = "ALERT: ML Feature Distribution Drift Detected"
  combiner     = "OR"

  conditions {
    display_name = "Canary feature PSI >= ${var.psi_drift_threshold}"

    condition_threshold {
      filter          = "metric.type=\"workload.googleapis.com/bqml.drift.feature_psi\" AND resource.type=\"generic_task\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.psi_drift_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["metric.label.feature"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      The Population Stability Index for the canary feature exceeded
      ${var.psi_drift_threshold}. The pipeline circuit breaker halted scoring
      before any predictions were written.

      **Triage**
      1. Confirm the upstream load for the target partition completed fully.
      2. Compare the feature distribution against the baseline window.
      3. If the shift is legitimate (a real change in the world rather than a
         data defect), move the baseline window forward and retrain. Do not
         simply raise the threshold.
    EOT
  }
}

# ------------------------------------------------------------------------------
# Warehouse cost anomaly
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "high_slot_usage" {
  count        = var.enable_alert_policies ? 1 : 0
  display_name = "WARN: BQML Batch Inference High Slot Usage"
  combiner     = "OR"

  conditions {
    display_name = "Slot milliseconds > ${var.max_slot_millis_warning} per run"

    condition_threshold {
      filter          = "metric.type=\"workload.googleapis.com/bqml.inference.slot_millis\" AND resource.type=\"generic_task\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.max_slot_millis_warning
      duration        = "0s"

      aggregations {
        alignment_period = "300s"
        # ALIGN_DELTA, not ALIGN_SUM. slot_millis is an OpenTelemetry Counter,
        # which Cloud Monitoring stores as CUMULATIVE INT64, and the API rejects
        # ALIGN_SUM on that kind with HTTP 400. ALIGN_DELTA turns the running
        # total into "slot-ms consumed in this window". The 404 on the missing
        # metric hid this on the first deploy; tests/test_terraform_contract.py
        # now checks every aligner against the instrument kind in telemetry.py.
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.model_name"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      Batch inference slot consumption exceeded the expected budget. Inspect the
      execution graph for the job in BigQuery Studio, and verify that partition
      pruning is still effective (an unpruned full-table scan is the usual
      cause).
    EOT
  }
}

# ------------------------------------------------------------------------------
# Run status: one email per run, success or failure, with a plain message
#
# The two metric policies above only fire on bad numbers, and only if the job
# ran far enough to emit them. These three log-match policies answer the
# simpler question an operator actually has: did tonight's run happen, and did
# it work? They watch logs rather than custom metrics, so they exist from the
# first apply and do not need enable_alert_policies. The recipient is
# var.notification_email; nothing here hardcodes an address.
# ------------------------------------------------------------------------------
locals {
  run_job_name      = google_cloud_run_v2_job.inference_job.name
  run_executions_ui = "https://console.cloud.google.com/run/jobs/details/${var.region}/${google_cloud_run_v2_job.inference_job.name}/executions?project=${var.project_id}"
}

# Every run ends with one structured "pipeline_run_summary" log entry written by
# src/run_summary.py. This policy turns it into an email whose subject carries
# the status and date, e.g. "[BQML batch] SUCCEEDED for 2026-10-06".
# tests/test_terraform_contract.py keeps the field names in step with the code.
resource "google_monitoring_alert_policy" "run_status" {
  display_name = "STATUS: BQML Batch Run Finished"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Pipeline wrote its run summary"

    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"${local.run_job_name}\"",
        "jsonPayload.event=\"pipeline_run_summary\"",
      ])
      label_extractors = {
        status         = "EXTRACT(jsonPayload.status)"
        target_date    = "EXTRACT(jsonPayload.target_date)"
        status_message = "EXTRACT(jsonPayload.status_message)"
        execution      = "EXTRACT(jsonPayload.execution)"
        exit_code      = "EXTRACT(jsonPayload.exit_code)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    # One email per run. The minimum period is 5 minutes, far shorter than the
    # daily schedule, so no real run is ever suppressed.
    notification_rate_limit {
      period = "300s"
    }
    # The shortest allowed, so each night's run opens a fresh incident.
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "[BQML batch] $${log.extracted_label.status} for $${log.extracted_label.target_date}"
    content   = <<-EOT
      **$${log.extracted_label.status_message}**

      - **Status:** $${log.extracted_label.status} (exit code $${log.extracted_label.exit_code})
      - **Target date:** $${log.extracted_label.target_date}
      - **Execution:** `$${log.extracted_label.execution}` ([all executions](${local.run_executions_ui}))

      **What to do**
      - `SUCCEEDED`: nothing. Predictions for the target date are in place.
      - `HALTED`: a guardrail (drift or a missing/short partition) stopped the
        run before any write. Do not retry; fix the input data, then re-run
        with `TARGET_DATE` set to the date above.
      - `FAILED`: an unexpected error (BigQuery 5xx, quota, network). Writes are
        idempotent, so re-running the same `TARGET_DATE` is safe.

      This email is sent for every run. It closes itself after 30 minutes.
    EOT
  }
}

# Backstop for runs that end without writing a summary: the container crashed
# at startup (bad configuration, exit 1), was killed (signal, out of memory),
# or exited with a code the orchestrator never uses. Exit 0, 2 and 3 are left to
# the run-status email above, so a normal halt or failure sends one email, not two.
resource "google_monitoring_alert_policy" "job_execution_failed" {
  display_name = "ALERT: BQML Batch Job Crashed Without a Status"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "Cloud Run Job ended abnormally"

    condition_matched_log {
      # Parentheses are matched with [(] and [)], not \\( and \\): backslashes pass
      # through two layers of escaping (HCL, then the Logging query language), and
      # the escaped form deployed earlier never matched anything.
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"${local.run_job_name}\"",
        "log_id(\"run.googleapis.com/varlog/system\")",
        "(textPayload=~\"Container called exit[(](1|[4-9]|[1-9][0-9]+)[)]\" OR textPayload=~\"(?i)terminated on signal|memory limit\")",
      ])
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "[BQML batch] CRASHED: job ${local.run_job_name} ended without a run summary"
    content   = <<-EOT
      The nightly scoring job ended abnormally and could not report its own
      status, so predictions for the target date were **not** written.

      **Triage**
      - `exit(1)` right after start: the configuration failed validation. The
        first ERROR line in the execution's logs names the bad setting.
      - Killed by a signal or out of memory: check the task's memory limit in
        `terraform/cloud_run.tf` (keep it at 1 GiB or more).

      [Open the job's executions](${local.run_executions_ui})
    EOT
  }
}

# The scheduler could not start the job at all, so no run status will follow.
resource "google_monitoring_alert_policy" "scheduler_trigger_failed" {
  display_name = "ALERT: Scheduler Failed to Start the BQML Batch Job"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "Cloud Scheduler attempt finished with an error"

    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_scheduler_job\"",
        "resource.labels.job_id=\"${google_cloud_scheduler_job.batch_trigger.name}\"",
        "severity>=ERROR",
      ])
      label_extractors = {
        scheduler_status = "EXTRACT(jsonPayload.status)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "[BQML batch] NOT STARTED: scheduler error $${log.extracted_label.scheduler_status}"
    content   = <<-EOT
      Cloud Scheduler tried to start the nightly scoring job and the Cloud Run
      API rejected the call (**$${log.extracted_label.scheduler_status}**), so
      **no scoring ran** and no run-status email will follow.

      **Triage**
      1. `UNAUTHENTICATED` (401): the trigger must use an OAuth token, not OIDC,
         because the target is a `*.googleapis.com` API.
      2. `PERMISSION_DENIED` (403): the scheduler service account lost
         `roles/run.invoker` on the job.
      3. After fixing, run the missed date with
         `gcloud run jobs execute ${local.run_job_name} --region=${var.region} --update-env-vars=TARGET_DATE=YYYY-MM-DD`.
    EOT
  }
}
