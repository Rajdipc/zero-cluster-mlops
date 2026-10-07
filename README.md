# Zero-Cluster MLOps: BigQuery ML + Cloud Run Jobs + OpenTelemetry

[![GCP](https://img.shields.io/badge/Google%20Cloud-BigQuery%20ML%20%7C%20Cloud%20Run-4285F4?logo=google-cloud&logoColor=white)](#)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-Tracing%20%26%20Metrics-F5A800?logo=opentelemetry&logoColor=white)](#)
[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC?logo=terraform&logoColor=white)](#)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](#)

Production-grade batch ML inference executed **inside** BigQuery, orchestrated by an ephemeral Cloud Run Job, instrumented end-to-end with OpenTelemetry. No persistent clusters, no data movement tier.

**Contents**

| | |
| :--- | :--- |
| [TL;DR](#tldr) | the five commands |
| [Region vs. Location](#️-read-this-first-region-vs-location) | the most common first-run failure |
| [Prerequisites](#prerequisites) | IAM roles and APIs |
| [**Deploying from Google Cloud Shell**](#deploying-from-google-cloud-shell) | **full step-by-step runbook, nothing to install** |
| [Repository Structure](#repository-structure) | what every file does |
| [Data Model](#data-model) | partitioning, the canonical view, label validity |
| [Where Does the Data Come From?](#where-does-the-data-come-from) | the ETL boundary, and why Phase 0 exists |
| [**Data Notes**](docs/data-notes.md) *(separate page)* | the dataset, the two data bugs, and Phase 0 in full |
| [Configuration Reference](#configuration-reference) | every environment variable |
| [Verifying It Worked](#verifying-it-worked) | idempotency, circuit breaker, backfill |
| [Exit Codes](#exit-codes) | `0` / `2` / `3` and when to retry |
| [Troubleshooting](#troubleshooting) | symptom → cause → fix |
| [**Production Gotchas**](docs/production-gotchas.md) *(separate page)* | seven failure modes, the fix, the test, and a check for your deployment |
| [Known Limitations](#known-limitations) | what this deliberately does not do |
| [Cleanup](#cleanup) | tear it all down |

---

## TL;DR

```bash
export GCP_PROJECT_ID="your-project-id"
export GCP_REGION="us-central1"        # where the container runs
export BQ_LOCATION="US"                # where the data lives (NOT the same thing)
export NOTIFICATION_EMAIL="you@example.com"

make install && make test   # ~1 min, no cloud access needed
make seed                   # ~4 min, creates BQ objects + trains model
make docker-push            # ~3 min  (no Docker? make cloud-build-push)
make tf-apply ENABLE_ALERT_POLICIES=false   # ~2 min, first apply only
make execute                # ~1-2 min; first run also creates the custom metrics
make tf-apply               # adds the two metric alert policies, now that the metrics exist
```

Run `make help` at any time to see every target and your current settings.

> 🚀 **Prefer a browser?** There is a complete, copy-paste [**Google Cloud Shell runbook**](#deploying-from-google-cloud-shell) below — no local Docker, Terraform, or Python install required.

> [!CAUTION]
> **Three ordering constraints that are not obvious, and all produce confusing errors.**
>
> 1. **Terraform does not enable any APIs.** There is deliberately no `google_project_service` resource in `terraform/` — provisioning a project's API surface is usually a platform-team concern, not an application concern. You must run the `gcloud services enable` block yourself.
> 2. **`make seed` must run before `make tf-apply`.** Terraform's `google_bigquery_dataset_iam_member` grants `dataEditor` on the `ml_production` dataset, but the dataset is created by [`scripts/seed_and_train.sh`](scripts/seed_and_train.sh), not by Terraform. Apply first and you get `Error 404: Not found: Dataset <project>:ml_production`.
>
> 3. **The alert policies cannot be created until the job has run once.** They watch custom `workload.googleapis.com/bqml.*` metrics, which the OpenTelemetry exporter creates on the first execution. Before that, Cloud Monitoring rejects the policies with `Error 404: Cannot find metric(s) that match type`. Hence the first `tf-apply` passes `ENABLE_ALERT_POLICIES=false`, and the second one adds the alerts.
>
> The order in the TL;DR above is correct. Follow it on a first run.

> [!IMPORTANT]
> **Do you need to create a `.env` file? For deployment, no.**
>
> The commands above run entirely on the exported shell variables. `make seed`
> is a bash script that reads them directly; `make tf-apply` passes them to Terraform
> as `-var` flags; and the deployed container receives its settings as Cloud Run
> environment variables set by Terraform — it never sees a `.env` file at all.
>
> `.env` is a **local-development convenience only**. Create one when you want to run
> `make run-local` repeatedly without re-exporting variables in every new shell:
>
> ```bash
> cp .env.example .env     # then edit GCP_PROJECT_ID at minimum
> make run-local
> ```
>
> Precedence is: real environment variables **override** `.env`. The file is optional
> everywhere — [`scripts/run_local.sh`](scripts/run_local.sh) sources it only `if [[ -f .env ]]`,
> and Pydantic treats it as absent without complaint. It is in `.gitignore`; never commit it.

---

## ⚠️ Read This First: Region vs. Location

**`GCP_REGION` and `BQ_LOCATION` are different things and must not be made to match.**

| Variable | Value | Controls |
| :--- | :--- | :--- |
| `GCP_REGION` | `us-central1` | Where the Cloud Run Job container executes |
| `BQ_LOCATION` | `US` | Where the BigQuery dataset physically lives |

`bigquery-public-data` lives in the **`US` multi-region**, and BigQuery **cannot join across locations**. If you "helpfully" set `BQ_LOCATION=us-central1` to match your region, `make seed` fails with a location mismatch error that does not obviously point at the cause.

**Leave `BQ_LOCATION=US`.** This is the single most common first-run failure.

---

## Prerequisites

| Requirement | Check |
| :--- | :--- |
| Google Cloud SDK, authenticated | `gcloud auth list` |
| Application Default Credentials | `gcloud auth application-default login` |
| Python ≥ 3.11 | `python3 --version` |
| Docker **or** Cloud Build | `docker info`; without a daemon, `gcloud services enable cloudbuild.googleapis.com` and use `make deploy-cloudbuild` |
| Terraform ≥ 1.5 | `terraform version` |
| Billing enabled on the project | required for BigQuery queries |

### IAM roles you need on the project

`roles/owner` covers everything. If your project is locked down, request exactly this set — each one maps to a specific step, so a missing role shows up as a 403 several minutes into a run rather than upfront:

| Role | Needed for |
| :--- | :--- |
| `roles/serviceusage.serviceUsageAdmin` | enabling the eight APIs below |
| `roles/bigquery.admin` | creating the dataset, tables, view and BQML model (`make seed`) |
| `roles/artifactregistry.admin` | creating the repo and pushing the image (`make docker-push`) |
| `roles/run.admin` | creating and executing the Cloud Run Job |
| `roles/cloudscheduler.admin` | creating the nightly trigger |
| `roles/iam.serviceAccountAdmin` | creating the two service accounts |
| `roles/resourcemanager.projectIamAdmin` | binding the runner's four project-level roles |
| `roles/monitoring.admin` | the notification channel and five alert policies |

Enable the required APIs (one time, ~2 minutes to propagate):

```bash
gcloud services enable \
  bigquery.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudtrace.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com
```

| API | What this pipeline uses it for |
| :--- | :--- |
| `bigquery` | storage, `CREATE MODEL`, `ML.PREDICT`, `ML.EVALUATE` |
| `run` | the ephemeral orchestrator container |
| `cloudscheduler` | the 02:00 UTC nightly trigger |
| `cloudtrace` | OpenTelemetry span export — the execution waterfall |
| `monitoring` | custom PSI / ROC-AUC gauges and the five alert policies |
| `logging` | structured, trace-correlated JSON logs |
| `artifactregistry` | stores the container image |
| `aiplatform` | **optional** — see the note below |

> [!IMPORTANT]
> `aiplatform.googleapis.com` is required because `sql/train_model.sql` sets
> `model_registry = 'VERTEX_AI'` to register the model for lineage. Without it,
> `make seed` fails at the training step. If you would rather not enable Vertex
> AI, delete the `model_registry` and `vertex_ai_model_id` options from
> `sql/train_model.sql` — everything else works unchanged.

> [!NOTE]
> **Both auth steps are required.** `gcloud auth login` authenticates the CLI used by `make seed`. `gcloud auth application-default login` authenticates the Python client library used by `make run-local`. Doing only the first produces a confusing `DefaultCredentialsError`.
>
> **Exception: in Google Cloud Shell you need neither.** You are already authenticated, and Application Default Credentials resolve automatically via the Cloud Shell metadata server. See the runbook below.

---

## Deploying from Google Cloud Shell

The fastest way to run this end to end. Cloud Shell already has `gcloud`, `bq`, `terraform`, `docker` and Python pre-installed and pre-authenticated, so there is nothing to install on your machine.

**Total time: ~25 minutes**, most of it waiting on `make seed`.

### Step 1 — Get the code into Cloud Shell

```bash
git clone https://github.com/Rajdipc/zero-cluster-mlops.git
cd zero-cluster-mlops
```

<details>
<summary>No GitHub? Upload a tarball instead.</summary>

On your machine:

```bash
tar --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='terraform/.terraform' --exclude='*.tfstate*' \
    -czf ~/bqml-blueprint.tar.gz zero-cluster-mlops
```

Then in Cloud Shell: **⋮ (toolbar menu) → Upload**, select the tarball, and:

```bash
tar -xzf ~/bqml-blueprint.tar.gz && cd zero-cluster-mlops
```

Excluding `.venv` is not optional — it contains Python binaries built for your machine's interpreter version, which will not run in Cloud Shell. `make install` rebuilds it.

</details>

Verify the toolchain (all of these should already be present):

```bash
terraform version    # need >= 1.5.0
python3 --version    # need >= 3.11
docker version       # daemon should be running
```

### Step 2 — Set the project and environment

```bash
export GCP_PROJECT_ID="your-project-id"
export GCP_REGION="us-central1"          # where the CONTAINER runs
export BQ_LOCATION="US"                  # where the DATA lives — NOT the same thing
export NOTIFICATION_EMAIL="you@example.com"

gcloud config set project "$GCP_PROJECT_ID"
make help                                # prints what it is about to do; changes nothing
```

See [Read This First: Region vs. Location](#️-read-this-first-region-vs-location) for why those two are different.

> [!TIP]
> **Cloud Shell sessions expire** — 20 minutes idle, 12 hours maximum — and exported variables die with them. Append the four `export` lines to `~/.bashrc` so a reconnect does not silently leave `GCP_PROJECT_ID` unset.

### Step 3 — Enable the APIs

Run the `gcloud services enable` block from [Prerequisites](#prerequisites). Give it two minutes to propagate; a later `SERVICE_DISABLED` just means you were faster than the control plane.

You do **not** need `gcloud auth login` or `gcloud auth application-default login` here.

### Step 4 — Install and test offline

```bash
make install
make test
```

Expect **118 passed** in under a second. This touches no cloud resources and costs nothing — if it asks for credentials, your checkout is wrong. Stop and investigate rather than proceeding.

### Step 5 — Seed BigQuery and train  ⚠️ *first step that costs money*

```bash
make seed
```

Four things happen, in order:

1. **Creates the `ml_production` dataset** in location `US`, idempotently. *This is why seed must precede Terraform.*
2. Runs `sql/create_tables.sql` — feature table, the canonical `v_taxi_features` view, predictions table.
3. Loads three windows from `bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022`:
   * `2022-01-01` → `2022-01-15` — training **and** PSI baseline (~1.07 M rows after quality filters; ~830 K card trips reach the model)
   * source date `2022-02-01`, **remapped to yesterday** so the first scheduled run has something to score
   * `2022-02-10` — the backfill test partition (~105 K rows)
4. Runs `sql/train_model.sql` — logistic regression with a chronological `SEQ` split.

**~4 minutes. ~\$0.01.** It prints a partition summary — **confirm all three partitions are non-empty before continuing.** If the "yesterday" partition is empty, everything downstream fails for reasons that look unrelated.

### Step 6 — Build and push the image

```bash
make docker-push
```

Creates the Artifact Registry repo if absent, configures Docker auth, builds and pushes the image. **2–4 minutes.**

The tag is **derived from your commit**, not fixed. On a clean checkout it is the short SHA (e.g. `worker:51bbb57`); with uncommitted changes it gains a timestamp suffix (`worker:51bbb57-dirty-20260914190000`). Run `make help` to see the exact URI.

> [!IMPORTANT]
> A fixed tag like `v1.0.0` looks tidier and is a trap. Re-pushing the same tag leaves Terraform seeing an unchanged `container_image`, so it reports "No changes" and the Job keeps running the digest it resolved the first time. You change code, push, apply, execute — and observe the *old* behaviour while every command reports success.
>
> The cost of a commit-derived tag is that `make docker-push` and `make tf-apply` must see the same tag. If you commit or edit between the two, `tf-apply` will point at an image that was never pushed and Cloud Run will fail to start the task. That failure is loud and immediate, which is the point. To avoid it entirely, use one invocation:
>
> ```bash
> make deploy      # docker-push + tf-apply, guaranteed-matching tag
> ```

<details>
<summary>Docker unavailable or misbehaving? Use Cloud Build instead.</summary>

```bash
gcloud services enable cloudbuild.googleapis.com

make cloud-build-push    # same repo, same commit-derived tag, built remotely
make tf-apply            # finds the image because the tag is computed the same way
```

Or in one step, with a guaranteed-matching tag:

```bash
make deploy-cloudbuild   # cloud-build-push + tf-apply
```

This is how the reference deployment was built: the workstation it ran on had no Docker daemon. A first build takes about a minute.

</details>

### Step 7 — Provision the infrastructure

```bash
make tf-plan     # optional on a first run, but worth reading
make tf-apply ENABLE_ALERT_POLICIES=false
```

Sixteen resources in total, ~2 minutes. This first apply creates fourteen of them, including the three log-based status and failure alerts; the two metric alert policies follow in Step 8, because their metrics do not exist until the job has run once:

| Resource | Detail |
| :--- | :--- |
| `google_cloud_run_v2_job` | `bqml-taxi-batch-worker` — 1 vCPU / 1 GiB, `max_retries = 0`, 1800 s timeout |
| `google_service_account` ×2 | `sa-bqml-batch-runner`, `sa-bqml-scheduler-invoker` |
| `google_project_iam_member` ×4 | `bigquery.jobUser`, `cloudtrace.agent`, `monitoring.metricWriter`, `logging.logWriter` |
| `google_bigquery_dataset_iam_member` | `dataEditor` scoped to **`ml_production` only** — deliberately not project-wide |
| `google_cloud_run_v2_job_iam_member` | `run.invoker` on **this one job only** |
| `google_cloud_scheduler_job` | `0 2 * * *` UTC, OAuth-authenticated (the target is a Google API, which rejects OIDC tokens) |
| `google_monitoring_notification_channel` | email → `$NOTIFICATION_EMAIL` |
| `google_monitoring_alert_policy` ×3 | **Run status** (log-based, created on the first apply): a status email after every run; job crashed without a status; scheduler could not start the job |
| `google_monitoring_alert_policy` ×2 | **Metrics** (created in Step 8): PSI drift > 0.25, slot-millis > 300000 |

**Then check the notification channel.** In the reference deployment the email channel worked straight away: the API reports no `verificationStatus` for it, meaning verification does not apply. If the console ever marks the channel **Unverified**, open *Monitoring → Alerting → Edit notification channels* and send a verification code, because an unverified channel delivers nothing.

> [!IMPORTANT]
> **Terraform state is local.** There is no `backend` block in `terraform/versions.tf`, so `terraform.tfstate` lands in `terraform/` inside your Cloud Shell home directory. Home persists between sessions but **Cloud Shell deletes it after 120 days of inactivity**. Lose the state and Terraform no longer knows these sixteen resources exist — you would delete them by hand or `terraform import` each one.
>
> For anything beyond a demo, add a remote backend:
>
> ```hcl
> # terraform/versions.tf
> terraform {
>   backend "gcs" {
>     bucket = "your-tfstate-bucket"
>     prefix = "bqml-blueprint"
>   }
> }
> ```

### Step 8 — Run it once, then add the metric alerts

```bash
make execute     # ~25 s of container time, ~1 min end to end
make tf-apply    # adds the two metric alert policies now that their metrics exist
```

The first execution is what creates the custom `workload.googleapis.com/bqml.*` metrics, so only now can Cloud Monitoring accept alert policies on them. If the second apply still reports `Cannot find metric(s)`, wait a few minutes and re-run it: new metric descriptors can take up to ten minutes to propagate.

Read the exit code — see [Exit Codes](#exit-codes). A first run exiting `2` almost always means the "yesterday" partition never landed; go back and re-read the Step 5 partition summary.

### Step 9 — Verify

Run the checks in [Verifying It Worked](#verifying-it-worked), then walk the console:

| Console page | What to look for |
| :--- | :--- |
| **BigQuery → `ml_production`** | feature table, `v_taxi_features`, `taxi_predictions`, and the `taxi_tip_model` node |
| **BigQuery → model → Evaluation** | ROC-AUC ≈ **0.77**. Above ~0.80 means target leakage is back and `total_amount` has crept into the feature list (the leaky variant measures 0.81) |
| **Cloud Run → Jobs → Executions** | one green execution, ~25 s |
| **Cloud Trace → Trace Explorer** | one `orchestrator.pipeline_run` trace (~10 s) with four child spans; click a span to see its BigQuery job ID, slot-ms and bytes billed as attributes |
| **Metrics Explorer** | the PSI and ROC-AUC gauges — allow ~60 s, the export interval is 60000 ms |
| **Cloud Scheduler** | `trigger-bqml-taxi-batch-scoring`, next run 02:00 UTC |

Optionally run the orchestrator directly from Cloud Shell, which is also how you backfill:

```bash
TARGET_DATE=2022-02-10 make run-local
```

### Cloud Shell troubleshooting

These are in addition to the [main troubleshooting table](#troubleshooting):

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `Error 404: Not found: Dataset ...:ml_production` during apply | Terraform ran before `make seed` | Run `make seed`, then re-apply |
| `Error 404: Cannot find metric(s) that match type = "workload.googleapis.com/bqml..."` | Alert policies applied before the job ever ran | `make tf-apply ENABLE_ALERT_POLICIES=false`, `make execute`, then `make tf-apply` |
| `Error 400: ... "ALIGN_SUM": The aligner cannot be applied to metrics with kind CUMULATIVE` | Checkout predates the aligner fix | Pull latest; the slot alert now uses `ALIGN_DELTA` |
| `FATAL Flags parsing error: Unknown command line flag ' '` during `make seed` | Checkout predates the stdin fix; `bq` read the SQL's `--` banner as a flag | Pull latest |
| `Syntax error: Unexpected keyword ROWS` | `ROWS` is reserved in GoogleSQL | Alias the count as `row_count` |
| Job or repo created in the wrong project | `gcloud config` project differs from `GCP_PROJECT_ID` | Pull latest (every Makefile `gcloud` call now passes `--project`), or `gcloud config set project "$GCP_PROJECT_ID"` |
| `Cannot connect to the Docker daemon` | Cloud Shell VM was recycled | `make deploy-cloudbuild` (see Step 6) |
| `The project does not contain an App Engine application` | Older project without regional Cloud Scheduler | `gcloud app create --region=us-central`, then re-apply |
| Variables unset after reconnecting | Session expired | Put the exports in `~/.bashrc` |
| `no space left on device` during build | Cloud Shell disk filled with layers | `docker system prune -af`, or switch to Cloud Build |
| `ModuleNotFoundError` after upload | You copied a `.venv` built elsewhere | `rm -rf .venv && make install` |

### What it costs

| Item | Cost |
| :--- | :--- |
| `make seed` — one-time ~1 GB scan plus `CREATE MODEL` | ~\$0.01 |
| Each nightly run | fractions of a cent |
| Cloud Run Job — ~25 s/day at 1 vCPU / 1 GiB | well under \$0.01/month |
| Artifact Registry — one small image | ~\$0.01/month |
| BigQuery storage — ~1.2 M rows | pennies/month |
| Cloud Scheduler — three free jobs per account | \$0 |
| **Idle** | **\$0** — nothing runs between executions |

BigQuery's 1 TB/month free query tier and 10 GB free storage cover this entirely on most projects.

> [!WARNING]
> **Tear it down when you are finished.** Left alone, Cloud Scheduler fires every night at 02:00 UTC and demo ingestion scans the public dataset on each run. It is cents per month rather than dollars, but it is not zero, and it will keep emailing you when drift trips. See [Cleanup](#cleanup).

---

## Repository Structure

```
zero-cluster-mlops/
├── Dockerfile                  # Multi-stage build, non-root runtime user
├── Makefile                    # All developer automation -- start with `make help`
├── requirements.txt
├── .env.example                # Copy to .env for local runs
│
├── sql/
│   ├── create_tables.sql       # DDL: feature table, canonical view, predictions table
│   ├── ingest_demo_partition.sql # Phase 0: DEMO-ONLY synthetic partition load
│   ├── train_model.sql         # LOGISTIC_REG with chronological SEQ split
│   ├── calculate_psi.sql       # Push-down decile PSI vs. baseline window
│   ├── evaluate_model.sql      # ML.EVALUATE against realized production labels
│   ├── delete_partition.sql    # Idempotency, statement 1 of 2
│   └── batch_inference.sql     # Idempotency, statement 2 of 2 (ML.PREDICT)
│
├── src/
│   ├── config.py               # Pydantic v2 config, date-window arithmetic
│   ├── telemetry.py            # OTel setup + Cloud Trace-correlated JSON logging
│   ├── ingest.py               # Phase 0: DEMO-ONLY partition synthesis
│   ├── drift.py                # Pre-flight volume check + PSI circuit breaker
│   ├── evaluate.py             # Continuous evaluation against matured labels
│   ├── inference.py            # Idempotent two-job scoring + FinOps telemetry
│   ├── run_summary.py          # One plain-language status line per run (feeds the status email)
│   └── orchestrator.py         # Entrypoint, exit codes, run summary, shutdown flush
│
├── terraform/
│   ├── cloud_run.tf            # Cloud Run Job (max_retries, memory headroom)
│   ├── cloud_scheduler.tf      # Daily OAuth-authenticated trigger
│   ├── iam.tf                  # Dataset-scoped least-privilege identities
│   ├── monitoring.tf           # Run-status email + alert policies with triage runbooks
│   └── variables.tf / outputs.tf / provider.tf / versions.tf
│
├── scripts/
│   ├── seed_and_train.sh       # Idempotent: creates objects, loads data, trains
│   └── run_local.sh            # Local orchestrator execution
│
├── docs/
│   ├── data-notes.md           # Dataset, the two data bugs, Phase 0 in full
│   ├── production-gotchas.md   # Seven failure modes: fix, test, how to check
│   ├── blog/                   # Companion article (Markdown)
│   └── images/                 # Rendered diagrams and data tables
│
└── tests/                      # 118 tests, no cloud access required
    ├── helpers.py              # strip_sql_comments -- assertions must not match prose
    ├── test_feature_contract.py # cross-file SQL contract checks (see below)
    ├── test_terraform_contract.py # Terraform <-> container config seam
    ├── test_docs_contract.py   # stops this README drifting from the code
    ├── test_psi_bins.py        # parses the shipped PSI predicate and exercises it
    ├── test_orchestrator.py    # exit codes, halt-blocks-scoring, telemetry flush
    ├── test_evaluate.py        # warn-don't-halt, unlabeled partitions
    └── test_*.py               # config, drift, inference, telemetry, ingest
```

---

## Data Model

One partitioned feature table serves every role; the role is chosen by date window rather than by separate tables.

```
taxi_trips_features  (PARTITION BY scoring_date, CLUSTER BY vendor_id)
  ├── 2022-01-01 .. 2022-01-15  ->  training window AND PSI baseline window
  ├── 2022-02-10                ->  backfill test partition
  └── <yesterday>               ->  the partition the scheduled run scores
          |
          v
  v_taxi_features   <-- canonical feature contract (columns + quality predicates)
          |                 read by training, evaluation AND inference
          v
taxi_predictions     (PARTITION BY scoring_date, CLUSTER BY vendor_id)
  trip_id | scoring_date | vendor_id | predicted_is_high_tip
          | predicted_is_high_tip_probs | model_name | scored_at
```

Three deliberate choices:

1. **`trip_id`** — a synthesized surrogate key. Without it, predictions cannot be joined back to the trips they describe and the output table is write-only.
2. **`PARTITION BY scoring_date`** — the date whose data was scored, **not** `DATE(scored_at)`. Partitioning on wall-clock time would place a backfill of 2022-02-10 into today's partition.
3. **`v_taxi_features`** — one definition of the feature list and the quality filters, so training and inference cannot drift apart.

### Two label-validity decisions the schema does not show

Both came from profiling the public dataset before trusting it, and both change what the model means:

- **Cash trips are excluded (`payment_type = '1'`).** TLC records tips only for card payments, so every cash trip (22% of the January 2022 window) carries `tip_amount = 0.00`. The task is therefore *given a card payment, will the tip exceed \$2.00?*
- **`total_amount` is filtered on but never learned from.** It already contains the tip, so using it as a feature leaks the label. In our measurement the leak lifted holdout ROC-AUC from **0.77 to 0.81**, which is small enough to pass a review.

Both decisions are enforced by [`tests/test_feature_contract.py`](tests/test_feature_contract.py), which parses the SQL and fails if a filter references a column no write path populates — the bug that silently empties the view.

A related choice: `auto_class_weights` is off, because the card-only window is 61.7% positive and close to balanced.

> **The full story, with the profiling queries, the measured numbers and the checks you can reuse on your own data, is in [Data Notes](docs/data-notes.md).**

---

## Where Does the Data Come From?

This is the question most batch-scoring tutorials skip, and it is the one that breaks them on day two.

**This pipeline scores a partition. It does not own ingestion.** In a real deployment the boundary looks like this:

```
  [ your upstream ETL ]          [ this repo ]
  Dataflow / Datastream /        Phase 1  pre-flight + drift guardrail
  Fivetran / dbt / a           → Phase 2  ML.PREDICT into taxi_predictions
  scheduled query                Phase 3  continuous evaluation
        |                              ^
        v                              |
  taxi_trips_features  ────────────────┘
  (lands ~daily, before this job runs)
```

The pipeline's job is to **fail loudly** when the partition it was asked to score is missing. That is what `MIN_ROW_COUNT` and the pre-flight check in `src/drift.py` are for. Exit code `2`, no predictions written, and a `[BQML batch] HALTED` status email tells you why. Silently scoring a half-loaded partition would be far worse.

> [!WARNING]
> **The default `MIN_ROW_COUNT=1` is a presence check, not a volume floor.** It catches a partition that is entirely absent and nothing else; a partition at 10% of normal volume passes. That default exists because this repo cannot know your data. Once you do, raise it — the p50 row count per partition over the last 30 days, halved, is a reasonable starting point:
>
> ```bash
> make tf-apply MIN_ROW_COUNT=50000
> ```

### So why is there an ingestion phase in the code?

This demo reads `bigquery-public-data`, which is **frozen in 2022**, so nothing will ever land a partition for yesterday. Without help, the demo would work on day one and halt with `InsufficientDataException` on day two. **Phase 0** (`src/ingest.py` + `sql/ingest_demo_partition.sql`) closes that gap: when the target partition is absent, it synthesizes one deterministically from a rotating 28-day historical window, and a `NOT EXISTS` guard stops it from ever overwriting a partition your real ETL landed. The mechanism is explained in full in [Data Notes § 6](docs/data-notes.md#6-phase-0-keeping-the-demo-alive-past-day-one).

> [!WARNING]
> **Set `ENABLE_DEMO_INGESTION=false` in any real deployment.** A scoring pipeline that manufactures its own input data when the input is missing has replaced a loud failure with a silent lie. The only reason it is on by default here is that the alternative is a demo that breaks 24 hours after you deploy it.
>
> ```bash
> make tf-apply ENABLE_DEMO_INGESTION=false
> ```
>
> `make help` prints the current value, so you can confirm it before applying.

With demo ingestion disabled, a missing partition behaves the way it should: the pre-flight check halts the run with exit code `2` and the run-status alert in `terraform/monitoring.tf` emails you a `HALTED` message naming the empty partition.

---

## Configuration Reference

All values are settable via environment variable or `.env` for local runs.

The **Deploy** column is the part worth reading. ✅ means Terraform passes it to the Cloud Run Job and you can override it on the `make` command line; ⬜ means it is deliberately *not* a deploy-time knob, for the reason given. That distinction is enforced by [`tests/test_terraform_contract.py`](tests/test_terraform_contract.py), which fails if a new setting is added to `PipelineConfig` without someone deciding which it is.

| Variable | Default | Deploy | Notes |
| :--- | :--- | :---: | :--- |
| `GCP_PROJECT_ID` | — | ✅ | **Required.** |
| `GCP_REGION` | `us-central1` | ✅ | Cloud Run execution region. |
| `BQ_LOCATION` | `US` | ✅ | **Leave as `US`.** See the warning above. |
| `BQ_DATASET_ID` | `ml_production` | ✅ | |
| `MODEL_NAME` | `taxi_tip_model` | ✅ | Also the serving pointer for champion/challenger. |
| `PSI_DRIFT_THRESHOLD` | `0.25` | ✅ | `<0.10` none, `0.10–0.25` moderate, `>0.25` significant. |
| `MIN_ROW_COUNT` | `1` | ✅ | **A presence check at the default.** Raise it to catch partial loads. |
| `ENABLE_DEMO_INGESTION` | `true` | ✅ | **Set `false` in production.** Synthesizes the target partition when absent. |
| `DEMO_SOURCE_TABLE` | `...tlc_yellow_trips_2022` | ✅ | Public source table. One per year; **2011–2022 populated, 2023 is empty**. Year must match `DEMO_SOURCE_WINDOW_START`. |
| `DEMO_SOURCE_WINDOW_START` | `2022-02-01` | ✅ | First day of the window Phase 0 cycles through. Change it *with* the table above — the container validates the two agree and refuses to start otherwise. |
| `DEMO_SOURCE_WINDOW_DAYS` | `28` | ✅ | Length of that window. 28 rather than 30 so the rotation preserves day-of-week alignment. |
| `TARGET_DATE` | *(yesterday UTC)* | ⬜ | Must resolve per-execution. Pinning it in the Job would freeze every scheduled run onto one date — backfill with `--update-env-vars` instead. |
| `BASELINE_START_DATE` | `2022-01-01` | ⬜ | Must match the window the model was trained on, which `make seed` owns. |
| `BASELINE_END_DATE` | `2022-01-15` | ⬜ | Paired with the above. |
| `EVAL_LABEL_LAG_DAYS` | `0` | ⬜ | Domain property, not infrastructure. Raise in `config.py` for churn/fraud/credit. |
| `CANARY_FEATURE` | `fare_amount` | ⬜ | Changing it requires the feature to exist in the view and be numeric — a code change with a test. |
| `MIN_HOLDOUT_ROC_AUC` | `0.60` | ⬜ | Below this, a warning is emitted (not a halt). Belongs with the model, not the infra. |
| `FEATURES_TABLE` / `FEATURE_VIEW` / `PREDICTIONS_TABLE` | *(see `config.py`)* | ⬜ | Created by `make seed`; the Job cannot be pointed elsewhere without re-seeding. |
| `METRIC_EXPORT_INTERVAL_MILLIS` | `60000` | ⬜ | **Must be ≥ 10000.** A platform constraint, not a preference. See below. |
| `ENABLE_CLOUD_EXPORTERS` | `true` | ⬜ | Must be true in Cloud Run; `false` is for local runs that print telemetry to stdout. |

Deploy-time overrides all work the same way:

```bash
make tf-apply MIN_ROW_COUNT=50000 ENABLE_DEMO_INGESTION=false
```

> [!WARNING]
> **Do not lower `METRIC_EXPORT_INTERVAL_MILLIS`.** Cloud Monitoring rejects two points written to the same time series within 5 seconds. A short interval collides with the shutdown force-flush, and the failure is a *logged export error*, not a crash — your job goes green with incomplete metrics. The config enforces a floor of 10,000 ms and a unit test pins it.

---

## Verifying It Worked

### Predictions exist and are joinable

```sql
SELECT p.trip_id, p.predicted_is_high_tip,
       f.fare_amount, f.trip_distance, f.is_high_tip AS actual
FROM `ml_production.taxi_predictions` p
JOIN `ml_production.v_taxi_features` f USING (trip_id, scoring_date)
WHERE p.scoring_date = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
LIMIT 20;
```

### Idempotency: re-running must not duplicate

```bash
make execute && make execute
```

```sql
SELECT scoring_date, COUNT(1) AS row_count, COUNT(DISTINCT trip_id) AS unique_trips
FROM `ml_production.taxi_predictions`
GROUP BY scoring_date ORDER BY scoring_date;
```

`row_count` must equal `unique_trips`.

### Circuit breaker: drift halts before any write

```bash
gcloud run jobs execute bqml-taxi-batch-worker --region="${GCP_REGION}" \
  --update-env-vars="PSI_DRIFT_THRESHOLD=0.0001" --wait
```

Expected: execution **Failed**, no retry, **zero** rows written, and the log line `Container called exit(2)`. The `gcloud` command itself returns `1` for any failed execution; the container's `2` is in the logs. The override applies to this one execution only; the Job keeps its configured threshold.

### Backfill lands in the right partition

```bash
gcloud run jobs execute bqml-taxi-batch-worker --region="${GCP_REGION}" \
  --update-env-vars="TARGET_DATE=2022-02-10" --wait
```

```sql
SELECT scoring_date, MIN(scored_at) AS first_scored_at, COUNT(1) AS row_count
FROM `ml_production.taxi_predictions`
GROUP BY scoring_date ORDER BY scoring_date;
```

`scoring_date = 2022-02-10` with a `scored_at` of today. Logical date in the partition key, wall clock in the audit column.

### Observability

* **Cloud Trace** → root span `orchestrator.pipeline_run` with **four** child spans in the default demo configuration: `ingest.demo_partition`, `drift.check_and_calculate_psi`, `evaluate.labeled_production_check`, `inference.execute_batch_predict`. With `ENABLE_DEMO_INGESTION=false` the first one disappears and you see three. Open `inference.execute_batch_predict` to see `bq.insert.slot_millis`, `bq.insert.rows_affected`, `bq.delete.job_id`.
* **Metrics Explorer** → `workload.googleapis.com/bqml.drift.feature_psi`, `bqml.evaluation.roc_auc`, `bqml.inference.slot_millis`.
* **Logs Explorer** → `resource.type="cloud_run_job"`; every entry carries `logging.googleapis.com/trace`.

### Status emails

Every run ends with one structured log line (`jsonPayload.event="pipeline_run_summary"`), and the **STATUS: BQML Batch Run Finished** policy emails it to `$NOTIFICATION_EMAIL`. The subject tells you the outcome before you open it:

| Subject | Meaning | What to do |
| :--- | :--- | :--- |
| `[BQML batch] SUCCEEDED for 2026-10-06` | Scored; the body gives rows, PSI and ROC-AUC | Nothing |
| `[BQML batch] HALTED for 2026-10-06` | A guardrail stopped the run before any write | Fix the input data; do not retry |
| `[BQML batch] FAILED for 2026-10-06` | Unexpected error (BigQuery 5xx, quota, network) | Re-run the same `TARGET_DATE`; writes are idempotent |
| `[BQML batch] CRASHED: ...` | The container died before it could report (bad config, OOM, signal) | Read the execution's first ERROR line |
| `[BQML batch] NOT STARTED: ...` | Cloud Scheduler could not start the job | See the troubleshooting row for `UNAUTHENTICATED` |

To see the summary lines yourself:

```
resource.type="cloud_run_job"
jsonPayload.event="pipeline_run_summary"
```

Emails come from `alerting-noreply@google.com`. If the first one lands in spam, mark it as not spam once. The recipient is whatever you pass as `NOTIFICATION_EMAIL`; nothing in the repo hardcodes an address.

---

## Exit Codes

| Code | Meaning | Retry helps? |
| :--- | :--- | :--- |
| `0` | Success | — |
| `2` | Guardrail halt (drift or insufficient data) | **No** — terminal by construction |
| `3` | Unexpected error (BigQuery 5xx, quota, network) | **Yes** — writes are idempotent |

Terraform ships `max_retries = 0`. Because Phase 3 clears the partition before repopulating it, raising this to `1` is safe if you prefer resilience to transient errors.

Why the codes are split this way, and a one-line check of the retry setting on your deployment, are in [Production Gotchas § 3](docs/production-gotchas.md#3-separating-terminal-guardrail-halts-from-transient-retries).

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `Dataset was not found in location us-central1` | Dataset created in the wrong location | `BQ_LOCATION=US`; drop and re-run `make seed` |
| `Not found: Table bigquery-public-data...tlc_yellow_trips_2022` | Public table unavailable in your region/version | Try `tlc_yellow_trips_2021` and adjust the date windows |
| `DefaultCredentialsError` | Missing ADC | `gcloud auth application-default login` |
| Training fails: `Vertex AI API has not been used` | `aiplatform.googleapis.com` not enabled | Enable it, or remove `model_registry` from `sql/train_model.sql` |
| `ERROR: NOTIFICATION_EMAIL is not set` | Required for the alert channel | `export NOTIFICATION_EMAIL=you@example.com` |
| `403 Permission bigquery.jobs.create denied` | APIs not enabled or billing off | Re-run the `gcloud services enable` block |
| Cloud Run task `OOMKilled` | Memory below 1 GiB | Keep `memory = "1024Mi"` |
| Metrics missing, job green | Export interval too short | Keep `METRIC_EXPORT_INTERVAL_MILLIS ≥ 10000` |
| `num_dml_affected_rows` is `None` | Query compiled as a script job | Keep runtime SQL single-statement |
| Duplicate prediction rows | Running v1 (append-only) | Ensure `delete_partition.sql` runs first |
| Job green on day 1, then `InsufficientDataException` every night | Nothing is landing new partitions | **Demo:** `ENABLE_DEMO_INGESTION=true`. **Production:** this is your ETL failing — fix it upstream, don't disable the check |
| `Demo ingestion misconfigured: DEMO_SOURCE_TABLE points at year ...` | Source table year and window year disagree | Set both to the same year. This is a startup guard, not a runtime failure — it is doing its job |
| Seeded successfully but every partition is empty | Pointed at `tlc_yellow_trips_2023`, which **exists but has 0 rows** | Use 2011–2022. Only those are populated |
| Predictions in today's partition after a backfill | Partitioned on `scored_at` | Partition on `scoring_date` |
| No execution at 02:00 UTC; scheduler log shows `UNAUTHENTICATED` / 401 | Scheduler sends an OIDC token to the Cloud Run Admin API | Use `oauth_token` with the `cloud-platform` scope (shipped in `terraform/cloud_scheduler.tf`). Full write-up: [Production Gotchas § 7](docs/production-gotchas.md#7-the-trigger-that-fires-but-never-runs) |
| Job failed but no email arrived | Only the drift and slot alerts existed; they need the job to emit metrics | The log-based alerts in `terraform/monitoring.tf` send a status email for every run, plus alerts for a crash or a missed start |

Several of these rows (location, metric interval, script jobs, backfill partition) are written up in full, with the code and a check you can run, in [Production Gotchas](docs/production-gotchas.md).

---

## Known Limitations

* **Drift coverage is narrow** — one numeric canary feature. Prediction drift and categorical PSI are natural additions.
* **Volume check is a static floor** — a rolling median over the trailing 7 days would catch partial loads.
* **Modest model quality is expected, and correct.** With `total_amount` removed as a target leak, the remaining features (vendor, passenger count, distance, fare) are genuinely weak predictors of tipping. A mediocre honest ROC-AUC is the right outcome; the pipeline is the subject of this repo, not the model.
* **No stateful circuit breaker** — retries are disabled rather than made cheap.
* **No CI/CD** — container builds are manual.

---

## Cleanup

```bash
make tf-destroy        # the 16 Terraform-managed resources (needs terraform/terraform.tfstate)

gcloud artifacts repositories delete bqml-batch-inference \
  --location="${GCP_REGION}" --project="${GCP_PROJECT_ID}" --quiet

# train_model.sql registers the model in Vertex AI; dropping the dataset does not remove it.
gcloud ai models delete taxi_tip_model \
  --region=us-central1 --project="${GCP_PROJECT_ID}" --quiet

# Only if you used make cloud-build-push / deploy-cloudbuild: Cloud Build's source staging bucket.
gcloud storage rm -r "gs://${GCP_PROJECT_ID}_cloudbuild/source/**"
```

> [!CAUTION]
> The following **irreversibly deletes the dataset**, including the trained model and all predictions, with no confirmation prompt.
> ```bash
> bq rm -r -f -d "${GCP_PROJECT_ID}:ml_production"
> ```
