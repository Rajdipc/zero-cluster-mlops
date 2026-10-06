# ==============================================================================
# Zero-Cluster BQML Pipeline -- developer automation
# ==============================================================================

.PHONY: help venv install test lint check-env check-deploy-env seed run-local \
        docker-build docker-push cloud-build-push tf-init tf-plan tf-apply \
        tf-destroy deploy deploy-cloudbuild execute clean

GCP_PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
GCP_REGION     ?= us-central1
BQ_LOCATION    ?= US
BQ_DATASET_ID  ?= ml_production
JOB_NAME       ?= bqml-taxi-batch-worker
REPO           ?= bqml-batch-inference

# ------------------------------------------------------------------------------
# Image tag: derived from the commit, never a fixed string.
#
# A constant tag like v1.0.0 is re-pushed on every build. Terraform then sees an
# unchanged container_image, reports "No changes", and the Cloud Run Job keeps
# running the digest it resolved the first time. You change code, push, apply,
# execute -- and observe the OLD behaviour while every command reports success.
# That failure is silent, and it is the worst kind of silent because all your
# tooling agrees with you.
#
# Clean tree  -> the short commit SHA. Reproducible and immutable.
# Dirty tree  -> SHA + timestamp, so an uncommitted experiment can never
#                overwrite the image that a commit already published.
# No git      -> timestamp only, for tarball downloads.
GIT_SHA     := $(shell git rev-parse --short HEAD 2>/dev/null)
GIT_DIRTY   := $(shell git diff --quiet HEAD 2>/dev/null || echo dirty)
BUILD_STAMP := $(shell date -u +%Y%m%d%H%M%S)
ifeq ($(GIT_SHA),)
IMAGE_TAG ?= build-$(BUILD_STAMP)
else ifeq ($(GIT_DIRTY),)
IMAGE_TAG ?= $(GIT_SHA)
else
IMAGE_TAG ?= $(GIT_SHA)-dirty-$(BUILD_STAMP)
endif

IMAGE_URI      ?= $(GCP_REGION)-docker.pkg.dev/$(GCP_PROJECT_ID)/$(REPO)/worker:$(IMAGE_TAG)
PY             ?= .venv/bin/python

# ------------------------------------------------------------------------------
# Runtime tunables forwarded to Terraform.
#
# These mirror fields on PipelineConfig. Terraform carries its own defaults, but
# a variable that can only be changed by editing a .tf file is, in practice, a
# variable nobody changes. Overriding on the command line must work:
#
#   make tf-apply ENABLE_DEMO_INGESTION=false MIN_ROW_COUNT=50000
#
# ENABLE_DEMO_INGESTION defaults to true so the public-dataset demo survives
# past its first night. SET IT FALSE FOR ANY REAL DEPLOYMENT -- populating the
# feature table is the upstream ETL's job, and a scoring pipeline that also
# ingests its own input cannot tell "upstream is late" from "upstream is broken".
ENABLE_DEMO_INGESTION    ?= true
DEMO_SOURCE_TABLE        ?= bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
DEMO_SOURCE_WINDOW_START ?= 2022-02-01
DEMO_SOURCE_WINDOW_DAYS  ?= 28
PSI_DRIFT_THRESHOLD      ?= 0.25
MIN_ROW_COUNT            ?= 1

# Defined once and reused by plan/apply/destroy. Three hand-maintained copies of
# the same list is how destroy quietly drifts out of sync with apply and then
# fails to tear down what apply created.
TF_VARS = -var="project_id=$(GCP_PROJECT_ID)" \
          -var="region=$(GCP_REGION)" \
          -var="bq_location=$(BQ_LOCATION)" \
          -var="bq_dataset_id=$(BQ_DATASET_ID)" \
          -var="container_image=$(IMAGE_URI)" \
          -var="notification_email=$(NOTIFICATION_EMAIL)" \
          -var="psi_drift_threshold=$(PSI_DRIFT_THRESHOLD)" \
          -var="min_row_count=$(MIN_ROW_COUNT)" \
          -var="enable_demo_ingestion=$(ENABLE_DEMO_INGESTION)" \
          -var="demo_source_table=$(DEMO_SOURCE_TABLE)" \
          -var="demo_source_window_start=$(DEMO_SOURCE_WINDOW_START)" \
          -var="demo_source_window_days=$(DEMO_SOURCE_WINDOW_DAYS)"

help:
	@echo "Setup"
	@echo "  make install       Create .venv and install dependencies"
	@echo "  make seed          Create BigQuery objects and train the model"
	@echo ""
	@echo "Develop"
	@echo "  make test          Run the unit test suite"
	@echo "  make run-local     Execute the orchestrator against BigQuery locally"
	@echo ""
	@echo "Deploy"
	@echo "  make docker-build  Build the container image"
	@echo "  make docker-push   Push to Artifact Registry (creates repo if needed)"
	@echo "  make tf-apply      Provision Cloud Run Job, Scheduler, IAM and alerts"
	@echo "  make deploy        docker-push + tf-apply with a guaranteed-matching tag"
	@echo "  make cloud-build-push   Build on Cloud Build instead (no local Docker)"
	@echo "  make deploy-cloudbuild  cloud-build-push + tf-apply"
	@echo "  make execute       Trigger one Cloud Run Job execution and wait"
	@echo ""
	@echo "Teardown"
	@echo "  make tf-destroy    Remove all Terraform-managed infrastructure"
	@echo "  make clean         Remove local caches and the virtualenv"
	@echo ""
	@echo "Current settings"
	@echo "  GCP_PROJECT_ID = $(GCP_PROJECT_ID)"
	@echo "  GCP_REGION     = $(GCP_REGION)   (Cloud Run)"
	@echo "  BQ_LOCATION    = $(BQ_LOCATION)              (BigQuery -- must be US)"
	@echo "  IMAGE_URI      = $(IMAGE_URI)"
	@echo ""
	@echo "Runtime tunables (override on the command line, e.g. MIN_ROW_COUNT=50000)"
	@echo "  ENABLE_DEMO_INGESTION = $(ENABLE_DEMO_INGESTION)   (set false for real deployments)"
	@echo "  DEMO_SOURCE_WINDOW_START = $(DEMO_SOURCE_WINDOW_START)  (year must match DEMO_SOURCE_TABLE)"
	@echo "  PSI_DRIFT_THRESHOLD   = $(PSI_DRIFT_THRESHOLD)"
	@echo "  MIN_ROW_COUNT         = $(MIN_ROW_COUNT)        (1 = presence check only)"

venv:
	@test -d .venv || python3 -m venv .venv

install: venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install pytest pytest-mock

test:
	$(PY) -m pytest tests/ -v

lint:
	@command -v terraform >/dev/null && terraform -chdir=terraform fmt -check -diff || \
		echo "terraform not installed; skipping HCL format check"
	$(PY) -m py_compile src/*.py tests/*.py
	@echo "OK"

# Fails fast with an actionable message rather than letting Terraform report a
# confusing downstream error about an empty notification channel label.
check-env:
	@if [ -z "$(GCP_PROJECT_ID)" ]; then \
		echo "ERROR: GCP_PROJECT_ID is not set."; \
		echo "       export GCP_PROJECT_ID=your-project-id"; \
		exit 1; fi
	@if [ "$(BQ_LOCATION)" != "US" ]; then \
		echo "ERROR: BQ_LOCATION is '$(BQ_LOCATION)', but must be 'US'."; \
		echo "       bigquery-public-data lives in the US multi-region and"; \
		echo "       BigQuery cannot join across locations."; \
		exit 1; fi
	@echo "Environment OK (project=$(GCP_PROJECT_ID), bq_location=$(BQ_LOCATION))"

check-deploy-env: check-env
	@if [ -z "$(NOTIFICATION_EMAIL)" ]; then \
		echo "ERROR: NOTIFICATION_EMAIL is not set."; \
		echo "       Terraform needs it to create the alert notification channel."; \
		echo "       export NOTIFICATION_EMAIL=you@example.com"; \
		exit 1; fi
	@echo "Deploy environment OK (notify=$(NOTIFICATION_EMAIL))"

seed: check-env
	GCP_PROJECT_ID=$(GCP_PROJECT_ID) BQ_LOCATION=$(BQ_LOCATION) \
	BQ_DATASET_ID=$(BQ_DATASET_ID) ./scripts/seed_and_train.sh

run-local:
	./scripts/run_local.sh

docker-build:
	docker build -t $(IMAGE_URI) .

# Every gcloud call below passes --project explicitly.
#
# GCP_PROJECT_ID can come from the environment, and bq/terraform honour it. A
# bare gcloud call does not: it uses `gcloud config get-value project`. When the
# two differ, the repo gets created -- and `make execute` runs -- in whatever
# project gcloud last pointed at. Found on the first real deploy.
docker-push: check-env
	@gcloud artifacts repositories describe $(REPO) --location=$(GCP_REGION) \
		--project=$(GCP_PROJECT_ID) >/dev/null 2>&1 || \
		gcloud artifacts repositories create $(REPO) \
			--repository-format=docker --location=$(GCP_REGION) \
			--project=$(GCP_PROJECT_ID)
	@gcloud auth configure-docker $(GCP_REGION)-docker.pkg.dev --quiet
	docker build -t $(IMAGE_URI) .
	docker push $(IMAGE_URI)

# Same result as docker-push, but the build runs on Cloud Build. Use it when the
# local Docker daemon is missing or broken (recycled Cloud Shell VMs, corporate
# workstations, CI runners without Docker-in-Docker).
cloud-build-push: check-env
	@gcloud artifacts repositories describe $(REPO) --location=$(GCP_REGION) \
		--project=$(GCP_PROJECT_ID) >/dev/null 2>&1 || \
		gcloud artifacts repositories create $(REPO) \
			--repository-format=docker --location=$(GCP_REGION) \
			--project=$(GCP_PROJECT_ID)
	gcloud builds submit --project=$(GCP_PROJECT_ID) --region=$(GCP_REGION) \
		--tag $(IMAGE_URI) .

tf-init:
	terraform -chdir=terraform init

tf-plan: check-deploy-env
	terraform -chdir=terraform plan $(TF_VARS)

tf-apply: check-deploy-env tf-init
	terraform -chdir=terraform apply -auto-approve $(TF_VARS)

tf-destroy: check-deploy-env
	terraform -chdir=terraform destroy -auto-approve $(TF_VARS)

# Push and apply in ONE make invocation.
#
# IMAGE_TAG is evaluated once per invocation, so this guarantees that the image
# Terraform points at is the image that was just pushed. Running `make
# docker-push` and `make tf-apply` as two separate commands is still fine, but
# if you edit or commit anything in between, the tag changes and apply will
# reference an image that was never pushed. Cloud Run then fails to start the
# task with an image-pull error -- loud and immediate, which is the point.
deploy: docker-push tf-apply
	@echo "Deployed $(IMAGE_URI)"

# deploy, minus the local Docker dependency.
deploy-cloudbuild: cloud-build-push tf-apply
	@echo "Deployed $(IMAGE_URI)"

execute:
	gcloud run jobs execute $(JOB_NAME) --region=$(GCP_REGION) \
		--project=$(GCP_PROJECT_ID) --wait

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .coverage htmlcov .venv
