"""Unit tests for ``snowtuner.experiments.sampling``.

The sampling layer is trust-load-bearing: bad samples produce bad
experiment reports.  Tests focus on the safety filter (``_has_unsafe_text``)
since that's the layer that decides whether a historical query is safe
to replay.
"""
from __future__ import annotations

from snowtuner.experiments.sampling import _has_unsafe_text


class TestUnsafeTextFilter:
    """The conservative substring check.  Errs on excluding queries that
    mention unsafe patterns even in a comment — false positives are
    safer than false negatives here."""

    def test_safe_query_passes(self):
        assert _has_unsafe_text("SELECT count(*) FROM users WHERE id = 42") is False

    def test_current_timestamp_blocked(self):
        # Time-dependent queries produce different results on replay,
        # which corrupts the paired comparison.
        assert _has_unsafe_text("SELECT CURRENT_TIMESTAMP() FROM t") is True

    def test_current_date_blocked(self):
        assert _has_unsafe_text("SELECT * FROM t WHERE created_at > CURRENT_DATE") is True

    def test_now_blocked(self):
        # The filter matches " now(" (with the leading space) — fast over
        # arbitrary identifier prefixes like "snow_func(" which legitimately
        # contain "now" but shouldn't trigger.
        assert _has_unsafe_text("SELECT now() FROM t") is True

    def test_information_schema_blocked(self):
        # Schema-snapshot reads aren't a workload signal — they reflect
        # the moment they ran, not the workload we want to measure.
        assert _has_unsafe_text(
            "SELECT * FROM information_schema.tables"
        ) is True

    def test_case_insensitive(self):
        assert _has_unsafe_text("SELECT CURRENT_TIMESTAMP() FROM t") is True
        assert _has_unsafe_text("select Current_TimeStamp() from t") is True

    def test_substring_match_in_comment(self):
        # Deliberately conservative: a comment mentioning the keyword
        # blocks the query.  False positive is the cheap outcome (one
        # less sample); false negative could poison the experiment.
        assert _has_unsafe_text(
            "-- updates the current_timestamp column\nSELECT 1"
        ) is True

    def test_empty_text_blocked(self):
        # Redacted / empty queries can't be replayed — block.
        assert _has_unsafe_text("") is True
        assert _has_unsafe_text(None) is True   # type: ignore[arg-type]

    def test_sysdate_blocked(self):
        # Snowflake supports sysdate() as an alias for current_timestamp.
        assert _has_unsafe_text("SELECT SYSDATE() FROM t") is True

    def test_localtimestamp_blocked(self):
        assert _has_unsafe_text("SELECT LOCALTIMESTAMP FROM t") is True
