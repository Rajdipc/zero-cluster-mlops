"""Behavioural tests for the PSI bin-membership predicate.

WHY THIS FILE EXISTS
--------------------
The bin predicate in calculate_psi.sql is four lines of boolean logic that
silently decide whether the circuit breaker can see drift at all. A substring
assertion ("does the SQL mention bin_id = 0?") would pass against a dozen
subtly wrong rewrites, so instead these tests EXTRACT the shipped predicate
from the SQL file, translate it into an equivalent Python expression, and
exercise it over real values.

That translation is deliberately narrow: it only understands the handful of
tokens this predicate uses, and it fails loudly rather than guessing if the
predicate is rewritten into a shape it does not recognise. A test that cannot
read the thing it is testing must not quietly report success.

THE BUG BEING GUARDED
---------------------
Bin edges come from the BASELINE's deciles. With closed outer edges, a scoring
row outside the baseline range joins to no bin: it drops out of the per-bin
numerator while remaining in the denominator, so PSI goes DOWN as the data
moves further away. Measured on a simulated shift, 30% of a partition could
leave the baseline range and still score 0.107 -- under the 0.25 halt.
"""

import re

import pytest

from src.config import PipelineConfig

# The baseline deciles used throughout. Evenly spaced so the expected bin for a
# given value is obvious by inspection.
PERCENTILES = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
BINS = [(i, PERCENTILES[i], PERCENTILES[i + 1]) for i in range(10)]


def _extract_join_predicates(sql: str) -> list:
    """Returns the ON predicate of every `JOIN bins b` in the SQL, comments stripped."""
    no_comments = "\n".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    )
    # From "JOIN bins b ... ON" up to the terminating GROUP BY.
    return [
        m.group(1).strip()
        for m in re.finditer(
            r"JOIN\s+bins\s+b\s+ON\s+(.*?)\s+GROUP\s+BY", no_comments, re.S | re.I
        )
    ]


def _compile_predicate(predicate: str):
    """Translates the SQL predicate into a callable f(value, bin_id, lo, hi) -> bool.

    Refuses to guess. Any token outside the known vocabulary raises, so a
    rewritten predicate surfaces as an error here rather than as a test that
    silently stops testing anything.
    """
    # The SQL predicate spans several indented lines; Python's parser would
    # reject that as an unexpected indent, so flatten it first.
    expr = re.sub(r"\s+", " ", predicate).strip()
    substitutions = [
        (r"\bd\.feature_val\b", "v"),
        (r"\bb\.min_val\b", "lo"),
        (r"\bb\.max_val\b", "hi"),
        (r"\bb\.bin_id\b", "i"),
        (r"\bAND\b", "and"),
        (r"\bOR\b", "or"),
        (r"\bNOT\b", "not"),
    ]
    for pattern, replacement in substitutions:
        expr = re.sub(pattern, replacement, expr, flags=re.I)

    # SQL `=` is Python `==`, but leave `>=`, `<=`, `!=` alone.
    expr = re.sub(r"(?<![<>!=])=(?!=)", "==", expr)

    allowed = re.compile(r"^[\sv()lohiandort0-9.<>=!]+$")
    assert allowed.match(expr), (
        f"Predicate uses tokens this test cannot interpret, so it can no longer "
        f"verify anything. Update the translator deliberately.\n  SQL: {predicate}\n"
        f"  translated: {expr}"
    )

    def evaluate(value, bin_id, lo, hi):
        return bool(eval(expr, {"__builtins__": {}}, {"v": value, "i": bin_id, "lo": lo, "hi": hi}))

    return evaluate


@pytest.fixture(scope="module")
def predicates(monkeypatch_session=None):
    import os

    os.environ.setdefault("GCP_PROJECT_ID", "p")
    sql = (PipelineConfig().sql_dir / "calculate_psi.sql").read_text(encoding="utf-8")
    found = _extract_join_predicates(sql)
    assert len(found) == 2, (
        f"Expected exactly two `JOIN bins` predicates (baseline_counts and "
        f"scoring_counts); found {len(found)}. If one side is binned differently "
        f"from the other, PSI is comparing incomparable distributions."
    )
    return [_compile_predicate(p) for p in found]


def _matching_bins(evaluate, value):
    return [i for i, lo, hi in BINS if evaluate(value, i, lo, hi)]


def test_baseline_and_scoring_use_an_identical_predicate():
    """Asymmetric binning would make PSI meaningless without failing anything."""
    import os

    os.environ.setdefault("GCP_PROJECT_ID", "p")
    sql = (PipelineConfig().sql_dir / "calculate_psi.sql").read_text(encoding="utf-8")
    baseline, scoring = _extract_join_predicates(sql)
    normalise = lambda s: re.sub(r"\s+", " ", s).strip()
    assert normalise(baseline) == normalise(scoring)


@pytest.mark.parametrize(
    "value,expected_bin",
    [
        (-1000.0, 0),   # far below the baseline minimum
        (-0.01, 0),     # just below the baseline minimum
        (0.0, 0),       # exactly the baseline minimum
        (9.99, 0),
        (10.0, 1),      # a bin edge belongs to the bin above it
        (55.0, 5),
        (89.99, 8),
        (90.0, 9),
        (100.0, 9),     # exactly the baseline maximum
        (100.01, 9),    # just above -- the case the closed-range version dropped
        (1e9, 9),       # far above
    ],
)
def test_every_value_lands_in_exactly_one_bin(predicates, value, expected_bin):
    for evaluate in predicates:
        hits = _matching_bins(evaluate, value)
        assert hits == [expected_bin], (
            f"value {value} matched bins {hits}, expected exactly [{expected_bin}]. "
            f"Zero matches means the row vanishes from the numerator while still "
            f"counting in the denominator, which pushes PSI DOWN as drift worsens. "
            f"Two matches double-counts it."
        )


def test_out_of_range_rows_are_never_dropped(predicates):
    """The regression itself: rows outside the baseline range must still be binned."""
    below = PERCENTILES[0] - 1
    above = PERCENTILES[-1] + 1
    for evaluate in predicates:
        assert _matching_bins(evaluate, below), "row below the baseline range was dropped"
        assert _matching_bins(evaluate, above), "row above the baseline range was dropped"


def test_interior_coverage_is_exhaustive_and_exclusive(predicates):
    """Sweep the whole range plus its margins; every value hits one bin."""
    values = [i * 0.37 - 20.0 for i in range(400)]
    for evaluate in predicates:
        for value in values:
            hits = _matching_bins(evaluate, value)
            assert len(hits) == 1, f"value {value} matched {len(hits)} bins"


def test_degenerate_quantiles_do_not_double_count(predicates):
    """Ties collapse bin edges. Bins may become empty, but must not overlap."""
    tied = [(i, 5.0, 5.0) for i in range(9)] + [(9, 5.0, 5.0)]
    for evaluate in predicates:
        for value in (4.9, 5.0, 5.1):
            hits = [i for i, lo, hi in tied if evaluate(value, i, lo, hi)]
            assert len(hits) == 1, (
                f"value {value} matched {len(hits)} degenerate bins; PSI would "
                f"count the same row several times"
            )
