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
