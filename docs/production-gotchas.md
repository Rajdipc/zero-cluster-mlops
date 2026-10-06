# Production Gotchas: The Full Write-Up

This runbook holds the detail behind the six "Production Gotchas" in the [Zero-Cluster MLOps blueprint](https://github.com/Rajdipc/zero-cluster-mlops). The [companion blog post](https://github.com/Rajdipc/zero-cluster-mlops/blob/main/docs/blog/zero-cluster-mlops-blueprint.md) gives a short summary of each one. Here you get, for each gotcha, what goes wrong, the fix with the shipped code, where the repository enforces it, and a command to confirm it on your own deployment.

The commands assume the default names from `terraform/variables.tf` (job `bqml-taxi-batch-worker`, dataset `ml_production`) and that `PROJECT_ID` and `GCP_REGION` are set as in the [Cloud Shell runbook](../README.md#deploying-from-google-cloud-shell).

**Contents**

1. [The wall-clock partition key trap](#1-the-wall-clock-partition-key-trap)
2. [The idempotency vs. telemetry tradeoff](#2-the-idempotency-vs-telemetry-tradeoff)
3. [Separating terminal guardrail halts from transient retries](#3-separating-terminal-guardrail-halts-from-transient-retries)
4. [Cloud Monitoring's 5-second write limit on short-lived jobs](#4-cloud-monitorings-5-second-write-limit-on-short-lived-jobs)
5. [`GCP_REGION` is not `BQ_LOCATION`](#5-gcp_region-is-not-bq_location)
6. ["Least privilege" that quietly grants the whole project](#6-least-privilege-that-quietly-grants-the-whole-project)
7. [Summary: where each fix is enforced](#7-summary-where-each-fix-is-enforced)

---

## 1. The wall-clock partition key trap

**What goes wrong.** Defining your predictions table with `CURRENT_TIMESTAMP() AS scored_at` and partitioning on `DATE(scored_at)` records *when the container ran* rather than *which day's data was scored*. The first time you backfill a historical date (say, `2022-02-10`), those rows land inside today's partition alongside today's scheduled run, corrupting downstream queries.

**The fix.** Partition explicitly on `scoring_date` (populated from the target-date parameter) and keep `scored_at` purely as an unpartitioned audit timestamp:

```sql
PARTITION BY scoring_date   -- the date whose data was scored
CLUSTER BY vendor_id
...
  scoring_date DATE NOT NULL,   -- written from the target-date parameter
  scored_at TIMESTAMP           -- audit only; never a partition key
```

**Where it is enforced.** The DDL in [`sql/create_tables.sql`](../sql/create_tables.sql) and the `scoring_date` projection in [`sql/batch_inference.sql`](../sql/batch_inference.sql). `test_explicit_target_date_used_for_backfill` in [`tests/test_config.py`](../tests/test_config.py) checks that an explicit `TARGET_DATE` replaces the default date, which is the value written to `scoring_date`.

**Check it yourself.** Run a backfill, then confirm it landed in its own partition rather than today's:

```bash
gcloud run jobs execute bqml-taxi-batch-worker --region="${GCP_REGION}" \
  --update-env-vars="TARGET_DATE=2022-02-10" --wait
```

```sql
SELECT scoring_date, MIN(scored_at) AS first_scored_at, COUNT(1) AS row_count
FROM `ml_production.taxi_predictions`
GROUP BY scoring_date ORDER BY scoring_date;
```

A `2022-02-10` row whose `first_scored_at` is today is the fix working: the data date and the run time are recorded separately.

---

## 2. The idempotency vs. telemetry tradeoff

**What goes wrong.** Plain `INSERT INTO ... SELECT` appends duplicate rows whenever a job is re-run. The natural SQL fix is to combine `DELETE` and `INSERT` in a single file:

```sql
DELETE FROM predictions WHERE scoring_date = @d;
INSERT INTO predictions SELECT ... FROM ML.PREDICT(...);
```

However, sending two semicolon-separated statements in one API call causes BigQuery to execute a **multi-statement script job**. For script jobs, the parent `QueryJob` object returns `num_dml_affected_rows = None`, wiping out the row-count telemetry on your OpenTelemetry spans.

**The fix.** Execute `delete_partition.sql` and `batch_inference.sql` as **two separate single-statement `client.query()` calls**. Each job returns exact `slot_millis`, `total_bytes_billed`, and `num_dml_affected_rows`:

```python
delete_job = client.query(config.load_sql("delete_partition.sql"))
delete_job.result()
delete_stats = _record_job_telemetry(delete_job, config, "delete")

insert_job = client.query(config.load_sql("batch_inference.sql"))
insert_job.result()
insert_stats = _record_job_telemetry(insert_job, config, "insert")
```

The same rule applies to DDL. Prefixing a runtime query with `CREATE TABLE IF NOT EXISTS ...;` also turns it into a script job, so all DDL runs once at setup (`make seed`), and tests check that the scoring and ingestion queries stay single-statement.

**Where it is enforced.** [`src/inference.py`](../src/inference.py). `test_runs_delete_and_insert_as_separate_jobs` in [`tests/test_inference.py`](../tests/test_inference.py) and `test_sql_is_single_statement_and_guarded` in [`tests/test_ingest.py`](../tests/test_ingest.py).

**Check it yourself.** In Cloud Trace, open the latest `orchestrator.pipeline_run` trace and select the `inference.execute_batch_predict` span. `bq.insert.rows_affected` should equal the number of rows scored, and on a re-run of the same date `bq.delete.rows_affected` should match the previous insert. If `bq.insert.rows_affected` reads `0` for a partition that clearly has data, a script job has crept back in (the code records a missing count as `0`).

---

## 3. Separating terminal guardrail halts from transient retries

**What goes wrong.** If Cloud Run is configured with `max_retries > 0` and your container exits with a generic non-zero code on drift, Cloud Run immediately spins up a retry container against the exact same bad partition, failing again and doubling alert noise. Conversely, setting `max_retries = 0` without distinguishing error types means a transient BigQuery 503 error leaves a 24-hour gap.

**The fix.** Use distinct exit codes so operators and orchestration tools can tell permanent data issues apart from infrastructure blips:

| Exit Code | Condition | Should Orchestration Retry? |
| :--- | :--- | :--- |
| `0` | Pipeline succeeded | N/A |
| `2` | **Guardrail halt** (`DriftDetectedException` or `InsufficientDataException`) | **No** (input data is bad; retrying reads the same partition) |
| `3` | **Unexpected runtime failure** (BigQuery 5xx, network blip, quota) | **Yes** (writes are idempotent, so retrying is safe) |

The job itself runs with `max_retries = 0`, so a drift halt is never retried automatically. An external orchestrator (Workflows, Composer, or a wrapper script) can safely retry exit `3`, because the write path is idempotent (gotcha 2). Raising `max_retries` to `1` is also safe for the same reason, but note that Cloud Run retries on any non-zero exit, so a drift halt would then run twice.

**Where it is enforced.** The exit-code constants and `finally` block in [`src/orchestrator.py`](../src/orchestrator.py); `max_retries = 0` in [`terraform/cloud_run.tf`](../terraform/cloud_run.tf). Tests: `test_guardrail_failures_exit_two`, `test_unexpected_failures_exit_three` and `test_flush_failure_does_not_mask_the_exit_code` in [`tests/test_orchestrator.py`](../tests/test_orchestrator.py), plus `test_documented_exit_codes_match_the_orchestrator` in [`tests/test_docs_contract.py`](../tests/test_docs_contract.py), which keeps the README's [Exit Codes](../README.md#exit-codes) table honest.

**Check it yourself.**

```bash
gcloud run jobs describe bqml-taxi-batch-worker --region="${GCP_REGION}" \
  --format="value(spec.template.spec.template.spec.maxRetries)"
# expect: 0
```

To see exit `2` for real, trip the drift circuit breaker as described in the blog's negative-testing section.

---

## 4. Cloud Monitoring's 5-second write limit on short-lived jobs

**What goes wrong.** Google Cloud Monitoring rejects two data points written to the same time series within **5 seconds** (`INVALID_ARGUMENT`). If `PeriodicExportingMetricReader` is configured with a short 5-second interval and your short job calls `meter_provider.shutdown()` at exit, the periodic background flush and the shutdown flush collide inside the 5-second window, dropping metrics with a non-fatal log warning while the job reports green.

**The fix.** Set `export_interval_millis = 60000` (longer than the job runtime so the shutdown flush is the sole export) and enable `add_unique_identifier=True`:

```python
# Extracted from: src/telemetry.py
gcp_metric_exporter = CloudMonitoringMetricsExporter(
    project_id=config.gcp_project_id,
    add_unique_identifier=True,      # prevents collisions across retried runs
)
metric_reader = PeriodicExportingMetricReader(
    gcp_metric_exporter,
    export_interval_millis=60000,    # 60s >> the ~25s job duration
)
```

**Where it is enforced.** In the shipped code the interval comes from `METRIC_EXPORT_INTERVAL_MILLIS` (default `60000`). [`src/config.py`](../src/config.py) refuses anything under 10 seconds, and `test_short_metric_interval_rejected` in [`tests/test_config.py`](../tests/test_config.py) guards that floor.

**Check it yourself.** In Cloud Logging, run:

```
resource.type="cloud_run_job"
jsonPayload.message:"unique_identifier=True"
```

Each execution logs `Initialized Cloud Monitoring metric exporter (export_interval=60000ms, unique_identifier=True).` Then confirm the custom metrics have one point per run in Metrics Explorer.

---

## 5. `GCP_REGION` is not `BQ_LOCATION`

**What goes wrong.** Setting both to `us-central1` feels natural. But `GCP_REGION` is where the container runs, and `BQ_LOCATION` is where the data lives. The public taxi data sits in the `US` multi-region and BigQuery cannot join tables across locations, so `BQ_LOCATION="us-central1"` makes the seed script fail immediately with a location mismatch error.

**The fix.** Keep two separate settings, `GCP_REGION="us-central1"` and `BQ_LOCATION="US"`. Terraform passes `BQ_LOCATION` to the container on its own and never derives it from the region.

**Where it is enforced.** The `BQ_LOCATION` env var in [`terraform/cloud_run.tf`](../terraform/cloud_run.tf), and `test_bigquery_client_uses_the_dataset_location_not_the_run_region` in [`tests/test_orchestrator.py`](../tests/test_orchestrator.py). The README covers the first-run symptom in [Region vs. Location](../README.md#️-read-this-first-region-vs-location).

**Check it yourself.**

```bash
gcloud run jobs describe bqml-taxi-batch-worker --region="${GCP_REGION}" --format=yaml \
  | grep -A1 -E "name: (GCP_REGION|BQ_LOCATION)"
# expect GCP_REGION -> us-central1 and BQ_LOCATION -> US
```

---

## 6. "Least privilege" that quietly grants the whole project

**What goes wrong.** Many reference templates claim least-privilege IAM while granting `roles/bigquery.dataEditor` at the **project** level, which lets the batch worker overwrite or delete any dataset in your Google Cloud project.

**The fix.** In [`terraform/iam.tf`](../terraform/iam.tf), only job submission and telemetry writing are granted at project scope. Data modification is restricted to the `ml_production` dataset:

```hcl
# terraform/iam.tf
# bigquery.jobUser is project-scoped by definition: creating a query job is a
# project-level operation with no dataset-level equivalent.
locals {
  runner_project_roles = [
    "roles/bigquery.jobUser",
    "roles/cloudtrace.agent",
    "roles/monitoring.metricWriter",
    "roles/logging.logWriter",
  ]
}

# Data access confined to ONE dataset.
resource "google_bigquery_dataset_iam_member" "runner_dataset_editor" {
  project    = var.project_id
  dataset_id = var.bq_dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runner_sa.email}"
}
```

**Where it is enforced.** [`terraform/iam.tf`](../terraform/iam.tf). The runner's project-level roles are an explicit list, and `roles/bigquery.dataEditor` appears only on the dataset resource.

**Check it yourself.** The runner's project-level roles should be exactly these four, with no BigQuery data role:

```bash
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:sa-bqml-batch-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --format="value(bindings.role)"
# expect: roles/bigquery.jobUser, roles/cloudtrace.agent,
#         roles/logging.logWriter, roles/monitoring.metricWriter
```

The data access lives on the dataset instead. `bq show` lists it as the legacy `WRITER` role, which is how BigQuery displays `roles/bigquery.dataEditor` at dataset level:

```bash
bq show --format=prettyjson "${PROJECT_ID}:ml_production" | grep -B1 sa-bqml-batch-runner
```

---

## 7. Summary: where each fix is enforced

| Gotcha | Fix lives in | Guarded by |
| :--- | :--- | :--- |
| 1. Wall-clock partition key | [`sql/create_tables.sql`](../sql/create_tables.sql), [`sql/batch_inference.sql`](../sql/batch_inference.sql) | [`tests/test_config.py`](../tests/test_config.py) |
| 2. Script jobs lose row counts | [`src/inference.py`](../src/inference.py) | [`tests/test_inference.py`](../tests/test_inference.py), [`tests/test_ingest.py`](../tests/test_ingest.py) |
| 3. Exit codes and retries | [`src/orchestrator.py`](../src/orchestrator.py), [`terraform/cloud_run.tf`](../terraform/cloud_run.tf) | [`tests/test_orchestrator.py`](../tests/test_orchestrator.py), [`tests/test_docs_contract.py`](../tests/test_docs_contract.py) |
| 4. 5-second metric write limit | [`src/telemetry.py`](../src/telemetry.py), [`src/config.py`](../src/config.py) | [`tests/test_config.py`](../tests/test_config.py) |
| 5. Region vs. location | [`terraform/cloud_run.tf`](../terraform/cloud_run.tf) | [`tests/test_orchestrator.py`](../tests/test_orchestrator.py) |
| 6. Project-wide data role | [`terraform/iam.tf`](../terraform/iam.tf) | Manual check above |

All the tests run offline with `make test`.
