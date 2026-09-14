"""Contract tests for the feature-table write path.

These exist because of a real bug caught during review: the canonical view was
given a `payment_type = '1'` predicate while *no write path populated
payment_type*. Every unit test still passed, Terraform still validated, and the
SQL was still syntactically valid -- but the view would have returned zero rows
for every partition, and the first symptom would have been a confusing
InsufficientDataException in production.

The class of bug is general: a filter references a column that nothing fills in.
Unit tests that mock BigQuery cannot see it, because the breakage exists only in
the relationship *between* files. So these tests assert on that relationship
directly, by parsing the SQL.
"""

import pathlib
import re

import pytest

from tests.helpers import strip_sql_comments

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _read(path: pathlib.Path) -> str:
    return strip_sql_comments(path.read_text())


@pytest.fixture(scope="module")
def create_tables_sql() -> str:
    return _read(SQL_DIR / "create_tables.sql")


@pytest.fixture(scope="module")
def ingest_sql() -> str:
    return _read(SQL_DIR / "ingest_demo_partition.sql")


@pytest.fixture(scope="module")
def seed_sh() -> str:
    return _read(SCRIPTS_DIR / "seed_and_train.sh")


def _view_filter_columns(create_sql: str) -> set:
    """Columns referenced in the canonical view's WHERE clause."""
    view = create_sql[create_sql.index("CREATE OR REPLACE VIEW"):]
    where = view[view.index("WHERE"): view.index(";")]
    tokens = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", where))
    keywords = {
        "where", "and", "or", "not", "null", "is", "between", "in", "select",
        "from", "true", "false",
    }
    return tokens - keywords


def _projected_columns(sql: str, start_marker: str, end_marker: str) -> set:
    """Column aliases actually emitted by a projection.

    Substring matching is not good enough here: a column can appear in an inner
    CTE while being absent from the outer SELECT, which is exactly the shape of
    the bug these tests exist to catch. A mutation test (delete `payment_type`
    from the seed's outer projection) passes a naive `col in sql` check and
    fails this one.
    """
    body = sql[sql.index(start_marker) + len(start_marker): sql.index(end_marker)]
    columns = set()
    depth = 0
    current = ""
    for ch in body:  # split on top-level commas only, so IF(a, b, c) stays intact
        if ch in "(":
            depth += 1
        elif ch in ")":
            depth -= 1
        if ch == "," and depth == 0:
            columns.add(current.strip())
            current = ""
        else:
            current += ch
    columns.add(current.strip())
    # Reduce "expr AS alias" to the alias; bare columns are their own alias.
    return {re.split(r"\s+as\s+", c, flags=re.I)[-1].strip() for c in columns if c}


def test_every_view_filter_column_is_populated_by_seed(create_tables_sql, seed_sh):
    """A filter on a column the seed never projects silently empties the view."""
    projected = _projected_columns(seed_sh, ")\nSELECT", "\nFROM raw")
    for col in _view_filter_columns(create_tables_sql):
        assert col in projected, (
            f"The canonical view filters on '{col}', but the outer SELECT in "
            f"scripts/seed_and_train.sh does not project it, so the column will "
            f"be NULL and the view will return zero rows.\n"
            f"Projected: {sorted(projected)}"
        )


def test_every_view_filter_column_is_populated_by_phase0(create_tables_sql, ingest_sql):
    """Same contract, for the Phase 0 demo ingestion path."""
    projected = _projected_columns(ingest_sql, ")\nSELECT", "\nFROM candidate")
    for col in _view_filter_columns(create_tables_sql):
        assert col in projected, (
            f"The canonical view filters on '{col}', but the final SELECT in "
            f"sql/ingest_demo_partition.sql does not project it.\n"
            f"Projected: {sorted(projected)}"
        )


def test_ingest_insert_and_select_column_lists_match(ingest_sql):
    """A mismatch here is a runtime error, not a parse error."""
    insert_cols = [
        c.strip()
        for c in re.search(r"INSERT INTO [^(]+\((.*?)\)\s*WITH", ingest_sql, re.S)
        .group(1)
        .split(",")
        if c.strip()
    ]
    tail = ingest_sql[ingest_sql.rindex(")\nSELECT"):]
    select_cols = [
        c.strip()
        for c in tail[tail.index("SELECT") + 6: tail.index("FROM candidate")].split(",")
        if c.strip()
    ]
    assert insert_cols == select_cols, (
        f"INSERT targets {len(insert_cols)} columns but SELECT projects "
        f"{len(select_cols)}:\n  INSERT: {insert_cols}\n  SELECT: {select_cols}"
    )


def test_view_enforces_card_only_label_validity(create_tables_sql):
    """Cash trips carry a structurally-zero tip; including them is label noise."""
    assert "payment_type = '1'" in create_tables_sql, (
        "The canonical view must exclude non-card payments. NYC TLC does not "
        "record cash tips, so those rows carry is_high_tip = 0 regardless of "
        "trip characteristics (22% of the training window)."
    )


def test_total_amount_is_not_a_model_feature(create_tables_sql):
    """total_amount includes tip_amount, and the label derives from tip_amount."""
    view = create_tables_sql[create_tables_sql.index("CREATE OR REPLACE VIEW"):]
    projection = view[view.index("SELECT") + 6: view.index("FROM")]
    columns = {c.strip() for c in projection.split(",")}
    assert "total_amount" not in columns, (
        "total_amount leaks the target: it contains tip_amount, from which "
        "is_high_tip is derived. It may be filtered on, but never selected as "
        "a feature."
    )


def test_auto_class_weights_stays_disabled():
    """The label is near-balanced; weighting would break probability calibration."""
    train_sql = _read(SQL_DIR / "train_model.sql")
    assert "auto_class_weights" not in train_sql, (
        "auto_class_weights must stay unset. The card-only training window is "
        "61.7% positive, so there is no imbalance to correct, and enabling it "
        "would decalibrate the probabilities the use case multiplies by money."
    )
