"""Contract tests for SQL shown in documentation.

These exist because of a real defect found during a publication review: three
SQL snippets in the accompanying blog post still projected `total_amount` from
the canonical feature view, months after the target-leakage fix had removed that
column from the view. Every source file was correct. Every unit test passed.
Terraform validated. The only broken thing was the *documentation* -- and the
documentation is what readers copy and paste.

The failure mode is worth naming, because it is invisible to ordinary testing:

    A reader copies a query from the docs, runs it, and gets
      "Name total_amount not found inside project.dataset.v_taxi_features"
    ...while the repository it was supposedly extracted from works perfectly.

Docs drift silently because nothing executes them. So these tests parse the SQL
fences out of README.md and assert that any snippet reading from the canonical
feature view uses only columns the view actually projects.

Scope note: this guards README.md, which ships with the repository. The blog
post lives outside the repo and cannot be guarded from here -- it is checked by
hand against these same rules before publication.
"""

import pathlib
import re

import pytest

from tests.helpers import strip_sql_comments

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "sql"
README = REPO_ROOT / "README.md"

# Columns that must never be selected from the feature view, mapped to the
# reason. Keeping the reason next to the rule means a failure message explains
# itself instead of just naming a column.
FORBIDDEN_VIEW_COLUMNS = {
    "total_amount": (
        "total_amount contains tip_amount, and the label is derived from "
        "tip_amount. The view deliberately does not project it. Filtering on "
        "it in a WHERE clause is fine; selecting it is target leakage."
    ),
    "tip_amount": (
        "tip_amount IS the label source. Selecting it from the feature view "
        "would be direct leakage."
    ),
}


def _view_projection_columns() -> set:
    """Columns the canonical view actually exposes, parsed from the DDL."""
    sql = strip_sql_comments((SQL_DIR / "create_tables.sql").read_text())
    view = sql[sql.index("CREATE OR REPLACE VIEW"):]
    projection = view[view.index("SELECT") + len("SELECT"): view.index("FROM")]
    return {
        col.strip()
        for col in projection.split(",")
        if col.strip()
    }


def _sql_fences(markdown: str) -> list:
    """Returns (start_line, body) for every ```sql fence in a markdown file."""
    fences = []
    in_fence = False
    lang = ""
    start = 0
    buf = []

    for lineno, line in enumerate(markdown.split("\n"), start=1):
        if line.startswith("```"):
            if not in_fence:
                in_fence, lang, start, buf = True, line[3:].strip(), lineno, []
            else:
                in_fence = False
                if lang == "sql":
                    fences.append((start, "\n".join(buf)))
                buf = []
        elif in_fence:
            buf.append(line)

    return fences


def _reads_feature_view(body: str) -> bool:
    return "v_taxi_features" in body or "{feature_view}" in body


def _projection_of(body: str) -> str:
    """The SELECT list of a snippet, excluding anything after FROM.

    Deliberately naive but *conservative*: it stops at the first FROM, so
    WHERE-clause references are never mistaken for projected columns. That is
    the distinction the whole test rests on -- filtering on a column is allowed,
    selecting it is not.
    """
    clean = strip_sql_comments(body)
    upper = clean.upper()
    if "SELECT" not in upper:
        return ""
    start = upper.index("SELECT")
    end = upper.index("FROM", start) if "FROM" in upper[start:] else len(clean)
    return clean[start:end]


@pytest.fixture(scope="module")
def readme_sql_fences() -> list:
    return _sql_fences(README.read_text())


def test_readme_has_sql_examples_to_check(readme_sql_fences):
    """Guards the guard: a parser bug that finds nothing would pass silently."""
    assert readme_sql_fences, (
        "No ```sql fences found in README.md. Either the README lost its "
        "examples or _sql_fences() is broken -- both are real problems."
    )


def test_readme_snippets_never_select_leaked_columns(readme_sql_fences):
    """No documented query may SELECT a column the view refuses to expose."""
    violations = []

    for lineno, body in readme_sql_fences:
        if not _reads_feature_view(body):
            continue
        projection = _projection_of(body)
        for column, reason in FORBIDDEN_VIEW_COLUMNS.items():
            if re.search(rf"\b{re.escape(column)}\b", projection):
                violations.append(
                    f"README.md line {lineno}: projects '{column}' while "
                    f"reading the feature view. {reason}"
                )

    assert not violations, "Documentation leaks the target:\n  " + "\n  ".join(violations)


def test_readme_snippets_only_use_columns_the_view_exposes(readme_sql_fences):
    """A documented query that names a non-existent column fails at runtime.

    This is the defect that actually shipped: the snippet was valid SQL, read
    from a real view, and still could not execute -- because the column had been
    removed from the view and nobody re-read the docs.
    """
    exposed = _view_projection_columns()
    violations = []

    for lineno, body in readme_sql_fences:
        if not _reads_feature_view(body):
            continue

        # Only inspect qualified references (f.col), which unambiguously belong
        # to the view alias. Bare identifiers could come from any joined table.
        for alias_ref in re.findall(r"\b[a-z]\.([a-z_][a-z0-9_]*)\b", strip_sql_comments(body)):
            if alias_ref not in exposed and alias_ref in {
                c for c in FORBIDDEN_VIEW_COLUMNS
            } | {"dropoff_datetime", "payment_type"}:
                violations.append(
                    f"README.md line {lineno}: references '{alias_ref}', which "
                    f"the canonical view does not project. Exposed columns are: "
                    f"{sorted(exposed)}"
                )

    assert not violations, "Documentation references missing columns:\n  " + "\n  ".join(violations)


def test_view_projection_parser_finds_the_real_columns():
    """Pins the parser itself, so the tests above cannot silently degrade."""
    exposed = _view_projection_columns()

    assert "trip_id" in exposed, "parser lost the join key"
    assert "is_high_tip" in exposed, "parser lost the label"
    assert "pickup_datetime" in exposed, (
        "pickup_datetime must stay projected -- train_model.sql uses it as "
        "data_split_col and training fails without it."
    )
    assert "total_amount" not in exposed, (
        "total_amount is back in the view projection. That is the target leak."
    )


# ==============================================================================
# Numeric claims in the README.
#
# A later review found three stale numbers: the README advertised a test count
# that was two revisions old, claimed "three child spans" where the code emits
# four, and the blog post quoted a line count off by more than 2x. None of these
# break anything. All of them cost the reader trust, because they are the parts
# a reader can most easily check.
#
# Numbers that can be derived should be derived, and numbers that must be
# written down should be pinned to their source.
# ==============================================================================
def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_advertised_test_count_matches_reality():
    """The README tells readers what `make test` should print. Keep it true."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(REPO_ROOT / "tests")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match, f"could not parse collected count from pytest output:\n{result.stdout[-2000:]}"
    actual = int(match.group(1))

    claimed = set(int(n) for n in re.findall(r"\*\*(\d+) passed\*\*", _readme_text()))
    claimed |= set(int(n) for n in re.findall(r"#\s*(\d+) tests", _readme_text()))

    assert claimed, "README no longer states a test count; if that is deliberate, delete this test"
    assert claimed == {actual}, (
        f"README claims {sorted(claimed)} tests; pytest collects {actual}. "
        f"Update the README (both the `make test` expectation and the repo tree)."
    )


def test_advertised_child_span_count_matches_the_code():
    """`with tracer.start_as_current_span(...)` is countable. Count it."""
    src = REPO_ROOT / "src"
    spans = set()
    for path in src.glob("*.py"):
        if path.name == "orchestrator.py":
            continue  # the root span, not a child
        spans |= set(
            re.findall(r'start_as_current_span\(\s*"([^"]+)"', path.read_text(encoding="utf-8"))
        )

    assert len(spans) == 4, (
        f"expected 4 child spans, found {len(spans)}: {sorted(spans)}. "
        f"If a phase was added or removed, update the README's Observability section."
    )

    text = _readme_text()
    for name in spans:
        assert name in text, f"child span {name} is not documented in the README"

    assert "three child spans" not in text, (
        "README says 'three child spans' but the code emits four "
        "(ingest, drift, evaluate, inference) in the default configuration."
    )


def test_documented_exit_codes_match_the_orchestrator():
    """The troubleshooting table names exit codes by number."""
    from src.orchestrator import EXIT_GUARDRAIL_HALT, EXIT_OK, EXIT_UNEXPECTED

    text = _readme_text()
    assert f"`{EXIT_GUARDRAIL_HALT}`" in text, "the terminal exit code is not documented"
    assert f"`{EXIT_UNEXPECTED}`" in text, "the transient exit code is not documented"
    assert EXIT_OK == 0


def test_readme_does_not_advertise_a_fixed_image_tag():
    """A hard-coded tag in the docs re-creates the mutable-tag defect by example."""
    text = _readme_text()
    assert "worker:v1.0.0" not in text, (
        "README shows a fixed image tag. The Makefile derives the tag from the "
        "commit precisely so Terraform notices when the image changes; "
        "documenting a constant tag teaches readers the failure mode back."
    )
