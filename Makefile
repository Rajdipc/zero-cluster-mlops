# ==============================================================================
# Zero-Cluster BQML Pipeline -- developer automation
# ==============================================================================

.PHONY: help venv install test lint check-env check-deploy-env seed run-local \
        docker-build docker-push tf-init tf-plan tf-apply tf-destroy execute clean

GCP_PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
GCP_REGION     ?= us-central1
BQ_LOCATION    ?= US
BQ_DATASET_ID  ?= ml_production
JOB_NAME       ?= bqml-taxi-batch-worker
REPO           ?= bqml-batch-inference
IMAGE_TAG      ?= v1.0.0
IMAGE_URI      ?= $(GCP_REGION)-docker.pkg.dev/$(GCP_PROJECT_ID)/$(REPO)/worker:$(IMAGE_TAG)
PY             ?= .venv/bin/python

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

docker-push: check-env
	@gcloud artifacts repositories describe $(REPO) --location=$(GCP_REGION) >/dev/null 2>&1 || \
		gcloud artifacts repositories create $(REPO) \
			--repository-format=docker --location=$(GCP_REGION)
	@gcloud auth configure-docker $(GCP_REGION)-docker.pkg.dev --quiet
	docker build -t $(IMAGE_URI) .
	docker push $(IMAGE_URI)

tf-init:
	terraform -chdir=terraform init

tf-plan: check-deploy-env
	terraform -chdir=terraform plan \
		-var="project_id=$(GCP_PROJECT_ID)" -var="region=$(GCP_REGION)" \
		-var="bq_location=$(BQ_LOCATION)" -var="bq_dataset_id=$(BQ_DATASET_ID)" \
		-var="container_image=$(IMAGE_URI)" \
		-var="notification_email=$(NOTIFICATION_EMAIL)"

tf-apply: check-deploy-env tf-init
	terraform -chdir=terraform apply -auto-approve \
		-var="project_id=$(GCP_PROJECT_ID)" -var="region=$(GCP_REGION)" \
		-var="bq_location=$(BQ_LOCATION)" -var="bq_dataset_id=$(BQ_DATASET_ID)" \
		-var="container_image=$(IMAGE_URI)" \
		-var="notification_email=$(NOTIFICATION_EMAIL)"

tf-destroy: check-deploy-env
	terraform -chdir=terraform destroy -auto-approve \
		-var="project_id=$(GCP_PROJECT_ID)" -var="region=$(GCP_REGION)" \
		-var="bq_location=$(BQ_LOCATION)" -var="bq_dataset_id=$(BQ_DATASET_ID)" \
		-var="container_image=$(IMAGE_URI)" \
		-var="notification_email=$(NOTIFICATION_EMAIL)"

execute:
	gcloud run jobs execute $(JOB_NAME) --region=$(GCP_REGION) --wait

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .coverage htmlcov .venv
