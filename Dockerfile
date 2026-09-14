# ==============================================================================
# Multi-Stage Production Dockerfile for Serverless Batch Inference Worker
# ==============================================================================

# Stage 1: Build Dependencies
FROM python:3.11-slim AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Ephemeral Distroless/Slim Runtime
FROM python:3.11-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/appuser/.local/bin:$PATH \
    PYTHONPATH=/app

# Create non-root system user and group for security hardening
RUN groupadd -r appuser && useradd -r -g appuser -d /home/appuser -s /sbin/nologin appuser \
    && mkdir -p /home/appuser/.local && chown -R appuser:appuser /home/appuser

# Copy installed Python packages from builder
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application source code and SQL assets
COPY --chown=appuser:appuser src/ /app/src/
COPY --chown=appuser:appuser sql/ /app/sql/

USER appuser

ENTRYPOINT ["python", "-m", "src.orchestrator"]
