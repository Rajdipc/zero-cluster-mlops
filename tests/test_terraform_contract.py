"""Contract tests for the Terraform <-> container configuration seam.

WHY THIS FILE EXISTS
--------------------
Every other test in this suite passed while the deployed container could not
start. The container reads 23 settings from the environment; Terraform passed
8 of them. Worse, variables.tf *documented* a variable named
demo_source_window_start that had never been declared, so an operator following
the instruction hit a pydantic validation error telling them to set something
Terraform gave them no way to set.

Nothing catches that class of bug. `terraform validate` only sees HCL. pytest
only sees Python. The defect lives in the gap between them, which is precisely
where the expensive defects live.

These tests read both sides and assert they agree:

  * every PipelineConfig field is either wired into the Job or recorded below
    as a deliberate omission, with a reason;
  * coupled settings that validate against each other are wired together;
  * every `var.x` reference resolves to a declared variable, and every declared
    variable is actually used -- the mirror image of the original bug;
  * every variable without a default is supplied by the Makefile.

WHEN A TEST HERE FAILS
----------------------
You added a setting. Decide whether operators need to change it at deploy time.
If yes, wire it through variables.tf, cloud_run.tf and the Makefile. If no, add
it to INTENTIONALLY_NOT_DEPLOYED with the reason. Both are fine; silently doing
neither is not.
"""

import re
from pathlib import Path

import pytest

from src.config import PipelineConfig

TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "terraform"
MAKEFILE = Path(__file__).resolve().parent.parent / "Makefile"


# ==============================================================================
# Settings deliberately NOT exposed through Terraform, and why.
#
# The reason strings are not decoration. They are the record of a decision, and
# reviewing this dict is how the next person tells "chose not to" from "forgot".
# ==============================================================================
INTENTIONALLY_NOT_DEPLOYED = {
    "features_table": (
        "Object names are baked into the seed script's DDL. Renaming one here "
        "without re-seeding points the Job at a table that does not exist."
    ),
    "feature_view": (
        "Same as features_table: the view is created by the seed script, not by "
        "the Job, so the Job cannot be pointed at a different one at deploy time."
    ),
    "predictions_table": (
        "Same as features_table: created by the seed script with a specific "
        "PARTITION BY scoring_date clause that the inference SQL depends on."
    ),
    "target_date": (
        "Must resolve to 'yesterday' at each execution. Pinning it in the Job "
        "definition would freeze every scheduled run onto one date. Backfills "
        "override it per-execution with --update-env-vars instead."
    ),
    "baseline_start_date": (
        "The PSI baseline window must match the window the model was trained on, "
        "which the seed script owns. Changing it here alone would compare "
        "today's data against a period the model never saw."
    ),
    "baseline_end_date": "Paired with baseline_start_date; same reasoning.",
    "eval_label_lag_days": (
        "0 is correct for taxi tips, where the label is known at trip completion. "
        "Domains with maturing labels should change the default in config.py as "
        "part of adapting the blueprint, not per-deployment."
    ),
    "canary_feature": (
        "Changing it requires the feature to exist in the view AND to be "
        "numeric, so it is a code change with a test, not a deploy-time knob."
    ),
    "min_holdout_roc_auc": (
        "Warn-only threshold. Its sensible value depends on the retrained "
        "model, so it belongs with the model definition rather than the "
        "infrastructure."
    ),
    "enable_cloud_exporters": (
        "Must be true in Cloud Run. The false path exists only so local runs "
        "print spans to the console instead of requiring Cloud Trace access."
    ),
    "metric_export_interval_millis": (
        "60000 satisfies Cloud Monitoring's 5-second per-time-series write "
        "limit. That is a platform constraint, not a preference, and the "
        "config-level ge=10000 validator already guards it."
    ),
    "sql_dir": "A path inside the image, fixed by the Dockerfile's COPY.",
}


def _read_terraform() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TERRAFORM_DIR.glob("*.tf"))
    )


def _strip_hcl_comments(text: str) -> str:
    """Removes `#` and `//` comments so prose cannot satisfy a code assertion.

    This matters here more than usual: the bug that prompted this file was a
    variable that existed *only* in a comment.
    """
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(
        re.split(r"#|//", line)[0] for line in without_block.splitlines()
    )


def _job_env_names() -> set:
    """Environment variable names passed to the Cloud Run Job container."""
    hcl = _strip_hcl_comments((TERRAFORM_DIR / "cloud_run.tf").read_text(encoding="utf-8"))
    return set(re.findall(r"env\s*\{\s*name\s*=\s*\"([A-Za-z0-9_]+)\"", hcl))


def _declared_variables() -> set:
    hcl = _strip_hcl_comments((TERRAFORM_DIR / "variables.tf").read_text(encoding="utf-8"))
    return set(re.findall(r'^variable\s+"([a-z0-9_]+)"', hcl, re.M))


def _referenced_variables() -> set:
    return set(re.findall(r"\bvar\.([a-z0-9_]+)\b", _strip_hcl_comments(_read_terraform())))


# ==============================================================================
# Config <-> Job environment
# ==============================================================================
def test_every_config_field_is_either_deployed_or_deliberately_omitted():
    fields = set(PipelineConfig.model_fields)
    wired = {name.lower() for name in _job_env_names()}
    omitted = set(INTENTIONALLY_NOT_DEPLOYED)

    undecided = fields - wired - omitted
    assert not undecided, (
        f"These PipelineConfig fields are neither passed by cloud_run.tf nor "
        f"recorded as deliberate omissions: {sorted(undecided)}. Wire them "
        f"through terraform, or add them to INTENTIONALLY_NOT_DEPLOYED with a "
        f"reason."
    )


def test_omission_list_has_no_phantom_entries():
    """A stale entry here would hide a genuinely unwired field behind an excuse."""
    fields = set(PipelineConfig.model_fields)
    phantom = set(INTENTIONALLY_NOT_DEPLOYED) - fields
    assert not phantom, f"INTENTIONALLY_NOT_DEPLOYED names fields that no longer exist: {sorted(phantom)}"


def test_omission_list_and_wiring_do_not_overlap():
    wired = {name.lower() for name in _job_env_names()}
    overlap = wired & set(INTENTIONALLY_NOT_DEPLOYED)
    assert not overlap, (
        f"{sorted(overlap)} are both wired into the Job and listed as omitted. "
        f"The list has gone stale and no longer documents reality."
    )


def test_every_job_env_var_maps_to_a_real_config_field():
    """An env var the container never reads is dead weight that looks load-bearing."""
    fields = set(PipelineConfig.model_fields)
    unknown = {name for name in _job_env_names() if name.lower() not in fields}
    assert not unknown, f"cloud_run.tf sets env vars PipelineConfig ignores: {sorted(unknown)}"


def test_omissions_all_carry_a_reason():
    for field, reason in INTENTIONALLY_NOT_DEPLOYED.items():
        assert reason and len(reason) > 30, f"{field} needs a real explanation, not a placeholder"


# ==============================================================================
# Coupled settings
# ==============================================================================
def test_demo_window_is_wired_alongside_the_demo_table():
    """THE ORIGINAL BUG.

    PipelineConfig cross-validates the year in DEMO_SOURCE_TABLE against the
    year in DEMO_SOURCE_WINDOW_START and refuses to start on a mismatch.
    Deploying one without the other means an operator can trip that validator
    with no way to satisfy it: the error names a variable Terraform does not
    expose.
    """
    env = _job_env_names()
    if "DEMO_SOURCE_TABLE" in env:
        assert "DEMO_SOURCE_WINDOW_START" in env, (
            "DEMO_SOURCE_TABLE is deployable but DEMO_SOURCE_WINDOW_START is not. "
            "The container validates them against each other, so they must be "
            "settable together."
        )


def test_the_default_demo_table_and_window_agree():
    """The shipped defaults must themselves satisfy the container's validator."""
    hcl = _strip_hcl_comments((TERRAFORM_DIR / "variables.tf").read_text(encoding="utf-8"))

    def default_of(name):
        block = re.search(
            rf'variable\s+"{name}"\s*\{{(.*?)\n\}}', hcl, re.S
        )
        assert block, f"variable {name} not found"
        value = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
        assert value, f"variable {name} has no string default"
        return value.group(1)

    table_year = re.search(r"(\d{4})\s*$", default_of("demo_source_table")).group(1)
    window_year = default_of("demo_source_window_start")[:4]
    assert table_year == window_year, (
        f"Terraform ships demo_source_table for {table_year} but "
        f"demo_source_window_start for {window_year}. The container rejects "
        f"that at startup, so the default configuration would not boot."
    )


# ==============================================================================
# Terraform internal consistency
# ==============================================================================
def test_every_referenced_variable_is_declared():
    missing = _referenced_variables() - _declared_variables()
    assert not missing, f"terraform references undeclared variables: {sorted(missing)}"


def test_every_declared_variable_is_used():
    """A declared-but-unused variable is a promise the configuration does not keep."""
    unused = _declared_variables() - _referenced_variables()
    assert not unused, (
        f"These variables are declared but never referenced: {sorted(unused)}. "
        f"Operators will set them and nothing will happen."
    )


# ==============================================================================
# Makefile <-> Terraform
# ==============================================================================
def test_makefile_supplies_every_variable_without_a_default():
    """Otherwise `make tf-apply` stops to prompt, which breaks non-interactive use."""
    hcl = _strip_hcl_comments((TERRAFORM_DIR / "variables.tf").read_text(encoding="utf-8"))
    blocks = re.findall(r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\n\}', hcl, re.S)
    required = {name for name, body in blocks if "default" not in body}

    makefile = MAKEFILE.read_text(encoding="utf-8")
    passed = set(re.findall(r'-var="([a-z0-9_]+)=', makefile))

    assert required - passed == set(), (
        f"variables with no default and no Makefile value: {sorted(required - passed)}"
    )


def test_makefile_only_passes_variables_that_exist():
    makefile = MAKEFILE.read_text(encoding="utf-8")
    passed = set(re.findall(r'-var="([a-z0-9_]+)=', makefile))
    unknown = passed - _declared_variables()
    assert not unknown, f"Makefile passes -var for undeclared variables: {sorted(unknown)}"


@pytest.mark.parametrize("target", ["tf-plan", "tf-apply", "tf-destroy"])
def test_terraform_targets_share_one_variable_list(target):
    """destroy must see the same variables as apply, or teardown drifts from setup."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    body = re.search(rf"^{re.escape(target)}:.*?(?=\n\w|\n#|\Z)", makefile, re.S | re.M)
    assert body, f"target {target} not found"
    assert "$(TF_VARS)" in body.group(0), (
        f"{target} hand-rolls its own -var list instead of reusing TF_VARS. "
        f"Three copies of one list is how they drift apart."
    )


# ==============================================================================
# Alert policies <-> OpenTelemetry instruments
#
# Found on the first real deploy. The slot-usage alert used ALIGN_SUM on
# bqml.inference.slot_millis, which telemetry.py creates as a Counter. Cloud
# Monitoring stores OTel counters as CUMULATIVE, and rejects ALIGN_SUM on a
# CUMULATIVE metric with HTTP 400 -- at apply time, never before. Neither
# `terraform validate` nor any Python test could see it, because the instrument
# kind lives in one language and the aligner in another.
# ==============================================================================
TELEMETRY_PY = Path(__file__).resolve().parent.parent / "src" / "telemetry.py"
SRC_DIR = TELEMETRY_PY.parent

# Aligners Cloud Monitoring accepts per metric kind (subset this repo can use).
# CUMULATIVE: only delta/rate-style aligners. GAUGE: anything except those.
_CUMULATIVE_ALIGNERS = {"ALIGN_DELTA", "ALIGN_RATE"}
_GAUGE_FORBIDDEN_ALIGNERS = {"ALIGN_DELTA", "ALIGN_RATE"}


def _instrument_kinds() -> dict:
    """Maps OTel metric name -> 'counter' | 'gauge', read from telemetry.py."""
    source = TELEMETRY_PY.read_text(encoding="utf-8")
    pairs = re.findall(r'create_(counter|gauge)\(\s*name\s*=\s*"([^"]+)"', source)
    assert pairs, "found no metric instruments in telemetry.py; has the API changed?"
    return {name: kind for kind, name in pairs}


def _alert_conditions() -> list:
    """(metric, aligner, group_by_labels) for every threshold condition."""
    hcl = _strip_hcl_comments((TERRAFORM_DIR / "monitoring.tf").read_text(encoding="utf-8"))
    conditions = []
    for block in re.split(r"condition_threshold\s*\{", hcl)[1:]:
        metric = re.search(r'metric\.type=\\"workload\.googleapis\.com/([^\\"]+)\\"', block)
        aligner = re.search(r'per_series_aligner\s*=\s*"([A-Z_]+)"', block)
        group_by = re.search(r"group_by_fields\s*=\s*\[([^\]]*)\]", block)
        assert metric and aligner, "an alert condition is missing its metric filter or aligner"
        labels = re.findall(r'"metric\.label\.([a-z_]+)"', group_by.group(1)) if group_by else []
        conditions.append((metric.group(1), aligner.group(1), labels))
    assert conditions, "found no alert conditions in monitoring.tf"
    return conditions


def test_every_alerted_metric_is_actually_emitted():
    """An alert on a metric nobody writes never fires, and never errors either."""
    kinds = _instrument_kinds()
    for metric, _, _ in _alert_conditions():
        assert metric in kinds, (
            f"monitoring.tf alerts on {metric}, which telemetry.py never creates."
        )


def test_alert_aligners_match_instrument_kinds():
    kinds = _instrument_kinds()
    for metric, aligner, _ in _alert_conditions():
        if kinds[metric] == "counter":
            assert aligner in _CUMULATIVE_ALIGNERS, (
                f"{metric} is an OTel Counter (CUMULATIVE in Cloud Monitoring) but "
                f"its alert uses {aligner}. The API rejects that with HTTP 400 at "
                f"apply time. Use one of {sorted(_CUMULATIVE_ALIGNERS)}."
            )
        else:
            assert aligner not in _GAUGE_FORBIDDEN_ALIGNERS, (
                f"{metric} is a Gauge but its alert uses {aligner}, which only "
                f"applies to CUMULATIVE or DELTA metrics."
            )


def test_alert_group_by_labels_are_recorded_as_attributes():
    """Grouping by a label the code never sets silently collapses every series."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in SRC_DIR.glob("*.py"))
    for metric, _, labels in _alert_conditions():
        for label in labels:
            assert re.search(rf'"{label}"\s*:', source), (
                f"alert on {metric} groups by metric.label.{label}, but no metric "
                f"attribute named '{label}' is ever recorded in src/."
            )


# ==============================================================================
# Run-status alert <-> run summary seam
# ==============================================================================
def _monitoring_tf() -> str:
    return (TERRAFORM_DIR / "monitoring.tf").read_text()


def test_run_status_alert_matches_the_summary_the_code_writes():
    """If the event name or a field name drifts, the status email silently stops
    (wrong filter) or arrives with blank fields (wrong extractor)."""
    from src.run_summary import ALERT_FIELDS, RUN_SUMMARY_EVENT

    tf = _monitoring_tf()
    block = tf[tf.index('resource "google_monitoring_alert_policy" "run_status"'):]
    block = block[:block.index("\nresource ")]
    assert f'jsonPayload.event=\\"{RUN_SUMMARY_EVENT}\\"' in block
    for name in ALERT_FIELDS:
        assert f"EXTRACT(jsonPayload.{name})" in block, name
        assert f"log.extracted_label.{name}" in block, name


def test_no_alert_recipient_is_hardcoded():
    """The repo is public; the only recipient is var.notification_email."""
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    for tf_file in TERRAFORM_DIR.glob("*.tf"):
        hits = [m for m in email.findall(tf_file.read_text())
                if not m.endswith((".iam.gserviceaccount.com", "example.com"))]
        assert not hits, f"{tf_file.name}: {hits}"
    assert "var.notification_email" in _monitoring_tf()


def test_scheduler_calls_the_run_api_with_an_oauth_token():
    """The :run URI is a Google API, which accepts only OAuth access tokens. An
    oidc_token applies cleanly and then fails every night with 401
    UNAUTHENTICATED, so the job never starts (docs/production-gotchas.md, #7)."""
    tf = re.sub(r"#.*", "", (TERRAFORM_DIR / "cloud_scheduler.tf").read_text())
    assert "run.googleapis.com" in tf
    assert "oauth_token" in tf
    assert "oidc_token" not in tf
    assert "https://www.googleapis.com/auth/cloud-platform" in tf
