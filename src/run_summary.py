"""One structured "run summary" log line per execution, for humans and alerting.

WHY THIS EXISTS
---------------
The pipeline already logs every phase, but an operator reading an alert email
needs one answer: did tonight's run work, and if not, what do I do? This module
builds that answer as a single JSON log entry at the end of every run:

    {"event": "pipeline_run_summary", "status": "SUCCEEDED",
     "status_message": "SUCCEEDED: scored 69,817 rows for 2026-10-06 ...", ...}

The Cloud Monitoring policy "BQML Batch Run Status" in terraform/monitoring.tf
matches on RUN_SUMMARY_EVENT and copies status, target_date, status_message and
execution into the email subject and body. tests/test_terraform_contract.py
keeps the field names on both sides in step.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# The alert policy filters on this exact value. Changing it here without
# changing terraform/monitoring.tf silently stops every status email.
RUN_SUMMARY_EVENT = "pipeline_run_summary"

STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_HALTED = "HALTED"
STATUS_FAILED = "FAILED"

# Fields the alert policy extracts into the email. Kept as a tuple so the
# Terraform contract test can assert every one is emitted.
ALERT_FIELDS = ("status", "target_date", "status_message", "execution", "exit_code")


@dataclass
class RunSummary:
    """Accumulates what happened during one execution."""

    target_date: str
    psi_threshold: float
    psi: Optional[float] = None
    roc_auc: Optional[float] = None
    rows_scored: Optional[int] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def build(self, exit_code: int, duration_s: float, error: Optional[BaseException] = None) -> Dict[str, Any]:
        """Returns the JSON fields for the summary log entry."""
        status = _status_for(exit_code)
        return {
            "event": RUN_SUMMARY_EVENT,
            "status": status,
            "exit_code": str(exit_code),
            "target_date": self.target_date,
            # Cloud Run Jobs sets CLOUD_RUN_EXECUTION; "local" for laptop runs.
            "execution": os.environ.get("CLOUD_RUN_EXECUTION", "local"),
            "duration_s": round(duration_s, 1),
            "rows_scored": self.rows_scored,
            "psi": None if self.psi is None else round(self.psi, 4),
            "psi_threshold": self.psi_threshold,
            "roc_auc": None if self.roc_auc is None else round(self.roc_auc, 4),
            "status_message": self._message(status, duration_s, error),
        }

    def _message(self, status: str, duration_s: float, error: Optional[BaseException]) -> str:
        took = _fmt_duration(duration_s)
        if status == STATUS_SUCCEEDED:
            rows = f"{self.rows_scored:,}" if self.rows_scored is not None else "0"
            parts = [f"SUCCEEDED: scored {rows} rows for {self.target_date} in {took}."]
            if self.psi is not None:
                parts.append(f"Drift PSI {self.psi:.4f} (threshold {self.psi_threshold}).")
            if self.roc_auc is not None:
                parts.append(f"ROC-AUC {self.roc_auc:.4f}.")
            else:
                parts.append("ROC-AUC not evaluated (labels not yet available).")
            return " ".join(parts)
        if status == STATUS_HALTED:
            return (
                f"HALTED: a guardrail stopped the run for {self.target_date} after {took}, "
                f"before any predictions were written. Reason: {error}. "
                "Do not retry; fix or reload the input partition first."
            )
        return (
            f"FAILED: unexpected error while processing {self.target_date} after {took}: "
            f"{type(error).__name__ if error else 'Error'}: {error}. "
            f"Writes are idempotent, so re-running TARGET_DATE={self.target_date} is safe."
        )


def _status_for(exit_code: int) -> str:
    if exit_code == 0:
        return STATUS_SUCCEEDED
    if exit_code == 2:
        return STATUS_HALTED
    return STATUS_FAILED


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"
