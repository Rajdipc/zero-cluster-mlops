# ==============================================================================
# Cloud Run Job: ephemeral serverless batch scoring worker
# ==============================================================================

resource "google_cloud_run_v2_job" "inference_job" {
  name     = var.job_name
  location = var.region

  template {
    task_count = 1
    template {
      # max_retries = 0.
      #
      # A drift halt is TERMINAL: the input partition is bad, so an immediate
      # retry reads the same data and fails identically, doubling cost and
      # duplicating alerts.
      #
      # The tradeoff is that genuinely transient failures (BigQuery 5xx, quota
      # blips) also lose their automatic retry, which for a daily job means a
      # 24-hour gap. The orchestrator therefore distinguishes the two cases by
      # exit code (2 = terminal guardrail halt, 3 = possibly transient), and all
      # writes are idempotent, so raising this to 1 is safe if you would rather
      # trade a little duplicate work for resilience.
      max_retries     = 0
      timeout         = "1800s"
      service_account = google_service_account.runner_sa.email

      containers {
        image = var.container_image

        resources {
          limits = {
            cpu = "1000m"
            # 1 GiB, not 512 MiB: serializing large BigQuery job metadata
            # alongside batched OTel spans causes transient spikes that
            # OOM-kill a 512 MiB container. At ~25s of runtime per day the
            # difference costs well under $0.01/month.
            memory = "1024Mi"
          }
        }

        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "GCP_REGION"
          value = var.region
        }
        # Deliberately distinct from GCP_REGION: the dataset lives in the US
        # multi-region so it can join against bigquery-public-data.
        env {
          name  = "BQ_LOCATION"
          value = var.bq_location
        }
        env {
          name  = "BQ_DATASET_ID"
          value = var.bq_dataset_id
        }
        env {
          name  = "MODEL_NAME"
          value = var.model_name
        }
        env {
          name  = "PSI_DRIFT_THRESHOLD"
          value = tostring(var.psi_drift_threshold)
        }
        # DEMO SCAFFOLDING. Set var.enable_demo_ingestion = false for real
        # deployments, where upstream ETL owns populating the feature table.
        env {
          name  = "ENABLE_DEMO_INGESTION"
          value = tostring(var.enable_demo_ingestion)
        }
        env {
          name  = "DEMO_SOURCE_TABLE"
          value = var.demo_source_table
        }
      }
    }
  }
}
