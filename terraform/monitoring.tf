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
# Run failures: the job did not start, or it exited non-zero
#
# The two policies above only see metrics the job emits while it runs. If the
# scheduler cannot start the job, or the job halts before it records a PSI above
# the alert threshold (a missing partition, an InsufficientDataException, a
# crash), nothing is emitted and nothing fires. These two log-match policies
# close that gap. They watch platform logs rather than custom metrics, so they
# exist from the first apply and do not need enable_alert_policies.
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "scheduler_trigger_failed" {
  display_name = "ALERT: Scheduler Failed to Start the BQML Batch Job"
  combiner     = "OR"

  conditions {
    display_name = "Cloud Scheduler attempt finished with an error"

    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_scheduler_job\"",
        "resource.labels.job_id=\"${google_cloud_scheduler_job.batch_trigger.name}\"",
        "severity>=ERROR",
      ])
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      Cloud Scheduler tried to start the nightly scoring job and the Cloud Run
      API rejected the call, so **no scoring ran tonight**.

      **Triage**
      1. Open the Cloud Scheduler job's logs and read `jsonPayload.status`.
      2. `UNAUTHENTICATED` (401): the trigger must use an OAuth token, not OIDC,
         because the target is a `*.googleapis.com` API.
      3. `PERMISSION_DENIED` (403): the scheduler service account lost
         `roles/run.invoker` on the job.
      4. After fixing, backfill the missed date with
         `--update-env-vars=TARGET_DATE=YYYY-MM-DD`.
    EOT
  }
}

resource "google_monitoring_alert_policy" "job_execution_failed" {
  display_name = "ALERT: BQML Batch Job Exited Non-Zero"
  combiner     = "OR"

  conditions {
    display_name = "Cloud Run Job container exited with a non-zero code"

    condition_matched_log {
      # Cloud Run writes "Container called exit(N)." to the system log for every
      # task. Match any N other than 0: 2 = guardrail halt, 3 = unexpected error.
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"${google_cloud_run_v2_job.inference_job.name}\"",
        "log_id(\"run.googleapis.com/varlog/system\")",
        "textPayload=~\"Container called exit\\\\([1-9][0-9]*\\\\)\"",
      ])
      label_extractors = {
        exit_message = "EXTRACT(textPayload)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      The nightly scoring job exited non-zero, so predictions for the target
      date were **not** written (or not fully written).

      **Triage**
      - `exit(2)`: guardrail halt. Search the execution's logs for
        `Guardrail halted the pipeline` to see whether it was drift or a missing
        or short partition. Do not retry; fix the input data.
      - `exit(3)`: unexpected failure (BigQuery 5xx, quota, network). Writes are
        idempotent, so re-running the same `TARGET_DATE` is safe.
    EOT
  }
}
