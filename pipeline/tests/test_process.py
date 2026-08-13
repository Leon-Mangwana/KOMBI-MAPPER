"""Tests for the batch runner.

What is under test here is the *orchestration*: which traces get picked up,
which status each outcome earns, what is written and what is deliberately not,
and what happens when OSRM goes away halfway through a backlog. The cleaning and
matching those steps call are covered by `test_clean.py` and `test_match.py`, so
they are stubbed out — that also keeps the suite free of a database, like the
rest of the pipeline tests.
"""

import logging

import pytest
from shapely.geometry import LineString

import process
from match import MatchedSegment, MatchResult, OSRMError


def match_result(fraction: float, *, length_m: float = 1000.0) -> MatchResult:
    """A `MatchResult` whose `matched_fraction` is `fraction`.

    Built as a straight line cut at the crossover so the fraction falls out of
    the real property rather than being asserted into place.
    """
    matched_m = length_m * fraction
    geometry = LineString([(0.0, 0.0), (length_m, 0.0)])

    segments = []
    if matched_m > 0.0:
        segments.append(
            MatchedSegment(
                geometry=LineString([(0.0, 0.0), (matched_m, 0.0)]),
                start_index=0,
                end_index=1,
                matched=True,
                confidence=0.9,
            )
        )
    if matched_m < length_m:
        segments.append(
            MatchedSegment(
                geometry=LineString([(matched_m, 0.0), (length_m, 0.0)]),
                start_index=1,
                end_index=2,
                matched=False,
                reason="no match",
            )
        )

    return MatchResult(geometry=geometry, segments=tuple(segments))


class Writes:
    """Records what a run wrote, standing in for the database."""

    def __init__(self) -> None:
        self.stored: list[int] = []
        self.cleared: list[int] = []
        self.statuses: list[tuple[int, str]] = []


@pytest.fixture()
def writes(monkeypatch):
    """Stub cleaning, loading and the three writers; record every write."""
    recorded = Writes()

    monkeypatch.setattr(process, "load_trace", lambda trace_id, engine=None: trace_id)
    monkeypatch.setattr(process, "clean_trace", lambda trace: [0] * 50)
    monkeypatch.setattr(
        process, "store_matched_geom", lambda tid, result, engine=None: recorded.stored.append(tid)
    )
    monkeypatch.setattr(
        process, "clear_matched_geom", lambda tid, engine=None: recorded.cleared.append(tid)
    )
    monkeypatch.setattr(
        process,
        "set_status",
        lambda tid, status, engine=None: recorded.statuses.append((tid, status)),
    )
    return recorded


@pytest.fixture()
def always_matches(monkeypatch):
    """Every trace matches fully."""
    monkeypatch.setattr(process, "match_trace", lambda cleaned, **kwargs: match_result(1.0))


ENGINE = object()  # a sentinel: nothing under test touches it


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (1.0, process.STATUS_MATCHED),
        (0.8, process.STATUS_MATCHED),
        (0.6, process.STATUS_MATCHED),  # the gate is inclusive
        (0.59, process.STATUS_REJECTED),
        (0.3, process.STATUS_REJECTED),
        (0.0, process.STATUS_REJECTED),
    ],
)
def test_classify_gates_on_matched_fraction(fraction, expected):
    assert process.classify(match_result(fraction), min_matched_fraction=0.6) == expected


def test_classify_honours_a_custom_gate():
    result = match_result(0.5)
    assert process.classify(result, min_matched_fraction=0.4) == process.STATUS_MATCHED
    assert process.classify(result, min_matched_fraction=0.9) == process.STATUS_REJECTED


# --------------------------------------------------------------------------
# One trace
# --------------------------------------------------------------------------


def test_matched_trace_is_stored_and_marked(writes, always_matches):
    outcome = process.process_trace(7, engine=ENGINE)

    assert outcome.status == process.STATUS_MATCHED
    assert outcome.ok
    assert outcome.reason is None
    assert outcome.matched_fraction == pytest.approx(1.0)
    assert outcome.points == 50
    assert writes.stored == [7]
    assert writes.statuses == [(7, process.STATUS_MATCHED)]
    assert writes.cleared == []


def test_rejected_trace_stores_no_geometry(writes, monkeypatch):
    monkeypatch.setattr(process, "match_trace", lambda cleaned, **kwargs: match_result(0.2))

    outcome = process.process_trace(8, engine=ENGINE)

    assert outcome.status == process.STATUS_REJECTED
    assert not outcome.ok
    assert "20%" in outcome.reason
    assert writes.stored == []
    assert writes.statuses == [(8, process.STATUS_REJECTED)]


def test_rejecting_a_trace_clears_geometry_an_earlier_run_stored(writes, monkeypatch):
    """A re-run that now rejects a trace must not leave the old line behind.

    `cluster.load_matched_traces` selects on `matched_geom IS NOT NULL` alone,
    so a stale geometry would keep a rejected trace in the clustering.
    """
    monkeypatch.setattr(process, "match_trace", lambda cleaned, **kwargs: match_result(0.1))

    process.process_trace(9, engine=ENGINE)

    assert writes.cleared == [9]


def test_trace_with_no_points_fails_without_matching(writes, monkeypatch):
    def missing(trace_id, engine=None):
        raise LookupError(f"Trace {trace_id} has no points.")

    monkeypatch.setattr(process, "load_trace", missing)
    monkeypatch.setattr(
        process, "match_trace", lambda *a, **k: pytest.fail("should not have matched")
    )

    outcome = process.process_trace(11, engine=ENGINE)

    assert outcome.status == process.STATUS_FAILED
    assert "no points" in outcome.reason
    assert outcome.matched_fraction is None
    assert writes.statuses == [(11, process.STATUS_FAILED)]
    assert writes.stored == []


def test_trace_with_unusable_timestamps_fails(writes, monkeypatch):
    def no_timestamps(trace):
        raise ValueError("Trace has points with no timestamp; cannot derive speeds.")

    monkeypatch.setattr(process, "clean_trace", no_timestamps)

    outcome = process.process_trace(12, engine=ENGINE)

    assert outcome.status == process.STATUS_FAILED
    assert "timestamp" in outcome.reason


def test_trace_too_short_after_cleaning_fails(writes, monkeypatch):
    monkeypatch.setattr(process, "clean_trace", lambda trace: [0, 0])
    monkeypatch.setattr(
        process, "match_trace", lambda *a, **k: pytest.fail("should not have matched")
    )

    outcome = process.process_trace(13, engine=ENGINE, min_points=5)

    assert outcome.status == process.STATUS_FAILED
    assert outcome.points == 2
    assert "survived cleaning" in outcome.reason


def test_dry_run_writes_nothing_but_reports_what_it_would_have(writes, always_matches):
    outcome = process.process_trace(14, engine=ENGINE, dry_run=True)

    assert outcome.status == process.STATUS_MATCHED
    assert writes.stored == []
    assert writes.cleared == []
    assert writes.statuses == []


def test_dry_run_records_no_status_for_a_failure(writes, monkeypatch):
    monkeypatch.setattr(process, "clean_trace", lambda trace: [])

    outcome = process.process_trace(15, engine=ENGINE, dry_run=True)

    assert outcome.status == process.STATUS_FAILED
    assert writes.statuses == []


def test_match_kwargs_reach_the_matcher(writes, monkeypatch):
    seen: dict = {}

    def spy(cleaned, **kwargs):
        seen.update(kwargs)
        return match_result(1.0)

    monkeypatch.setattr(process, "match_trace", spy)
    process.process_trace(16, engine=ENGINE, profile="car", timeout_s=5.0)

    assert seen == {"profile": "car", "timeout_s": 5.0}


# --------------------------------------------------------------------------
# A batch
# --------------------------------------------------------------------------


def test_a_batch_processes_every_trace(writes, always_matches):
    report = process.process_traces([1, 2, 3], engine=ENGINE)

    assert report.matched == 3
    assert report.aborted is None
    assert report.matched_ids == [1, 2, 3]
    assert writes.stored == [1, 2, 3]


def test_one_bad_trace_does_not_stop_the_batch(writes, monkeypatch):
    def sometimes(trace_id, engine=None):
        if trace_id == 2:
            raise LookupError("Trace 2 has no points.")
        return trace_id

    monkeypatch.setattr(process, "load_trace", sometimes)
    monkeypatch.setattr(process, "match_trace", lambda cleaned, **kwargs: match_result(1.0))

    report = process.process_traces([1, 2, 3], engine=ENGINE)

    assert (report.matched, report.failed) == (2, 1)
    assert report.matched_ids == [1, 3]
    assert report.aborted is None


def test_osrm_going_away_aborts_the_run(writes, monkeypatch, caplog):
    """OSRM being unreachable says nothing about the traces, so they keep their status."""
    calls: list[int] = []

    def flaky(cleaned, **kwargs):
        calls.append(len(calls))
        if len(calls) > 2:
            raise OSRMError("Connection refused.")
        return match_result(1.0)

    monkeypatch.setattr(process, "match_trace", flaky)

    with caplog.at_level(logging.ERROR):
        report = process.process_traces([1, 2, 3, 4, 5], engine=ENGINE)

    assert report.aborted == "Connection refused."
    assert report.matched == 2
    assert len(report.outcomes) == 2, "traces after the failure are left untouched"
    assert [tid for tid, _ in writes.statuses] == [1, 2]
    assert "stopping after 2 of 5" in caplog.text


def test_summary_counts_every_outcome(writes, monkeypatch):
    fractions = {1: 1.0, 2: 1.0, 3: 0.1}

    monkeypatch.setattr(
        process,
        "match_trace",
        lambda cleaned, **kwargs: match_result(fractions.pop(min(fractions))),
    )

    def sometimes(trace_id, engine=None):
        if trace_id == 4:
            raise LookupError("Trace 4 has no points.")
        return trace_id

    monkeypatch.setattr(process, "load_trace", sometimes)

    report = process.process_traces([1, 2, 3, 4], engine=ENGINE)

    assert (report.matched, report.rejected, report.failed) == (2, 1, 1)
    assert report.summary() == "4 trace(s) processed: 2 matched, 1 rejected, 1 failed"


def test_summary_names_the_abort():
    report = process.ProcessReport(outcomes=(), aborted="Connection refused.")
    assert "aborted: Connection refused." in report.summary()


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_run_selects_pending_traces_by_default(writes, always_matches, monkeypatch):
    seen: dict = {}

    def spy(engine=None, *, statuses=(), limit=None):
        seen["statuses"] = statuses
        seen["limit"] = limit
        return [4, 5]

    monkeypatch.setattr(process, "pending_trace_ids", spy)

    report = process.run(ENGINE)

    assert seen["statuses"] == (process.STATUS_UPLOADED,)
    assert report.matched_ids == [4, 5]


def test_retry_widens_the_selection_to_rejected_and_failed(writes, always_matches, monkeypatch):
    seen: dict = {}

    def spy(engine=None, *, statuses=(), limit=None):
        seen["statuses"] = statuses
        return []

    monkeypatch.setattr(process, "pending_trace_ids", spy)
    process.run(ENGINE, retry=True)

    assert set(seen["statuses"]) == {
        process.STATUS_UPLOADED,
        process.STATUS_REJECTED,
        process.STATUS_FAILED,
    }


def test_reprocess_takes_every_trace(writes, always_matches, monkeypatch):
    monkeypatch.setattr(
        process, "pending_trace_ids", lambda *a, **k: pytest.fail("should not have selected")
    )
    monkeypatch.setattr(process, "all_trace_ids", lambda engine=None, limit=None: [1, 2])

    assert process.run(ENGINE, reprocess=True).matched == 2


def test_explicit_ids_skip_selection_entirely(writes, always_matches, monkeypatch):
    monkeypatch.setattr(
        process, "pending_trace_ids", lambda *a, **k: pytest.fail("should not have selected")
    )

    assert process.run(ENGINE, trace_ids=[99]).matched_ids == [99]


def test_run_with_nothing_pending_is_an_empty_report(monkeypatch):
    monkeypatch.setattr(process, "pending_trace_ids", lambda *a, **k: [])

    report = process.run(ENGINE)

    assert report.outcomes == ()
    assert report.summary().startswith("0 trace(s)")


# --------------------------------------------------------------------------
# The full run
# --------------------------------------------------------------------------


def test_run_all_skips_clustering_when_nothing_matched(monkeypatch):
    """Re-clustering identical inputs costs minutes and writes the same routes back."""
    monkeypatch.setattr(process, "run", lambda engine=None, **kwargs: process.ProcessReport())
    monkeypatch.setattr(
        process, "get_engine", lambda: pytest.fail("should not have needed an engine")
    )

    import cluster

    monkeypatch.setattr(cluster, "run", lambda *a, **k: pytest.fail("should not have clustered"))

    assert process.run_all(ENGINE).outcomes == ()


def test_run_all_does_not_cluster_a_half_finished_backlog(monkeypatch):
    """An aborted run has matched an arbitrary prefix; publishing that as a feed is worse
    than publishing nothing new."""
    partial = process.ProcessReport(
        outcomes=(process.TraceOutcome(trace_id=1, status=process.STATUS_MATCHED),),
        aborted="Connection refused.",
    )
    monkeypatch.setattr(process, "run", lambda engine=None, **kwargs: partial)

    import cluster

    monkeypatch.setattr(cluster, "run", lambda *a, **k: pytest.fail("should not have clustered"))

    assert process.run_all(ENGINE).aborted == "Connection refused."


def test_run_all_clusters_and_exports_after_a_good_run(monkeypatch, tmp_path):
    import cluster
    import gtfs

    done = process.ProcessReport(
        outcomes=(process.TraceOutcome(trace_id=1, status=process.STATUS_MATCHED),)
    )
    monkeypatch.setattr(process, "run", lambda engine=None, **kwargs: done)

    called: dict = {}

    def fake_cluster(engine=None, replace_existing=False):
        called["replace"] = replace_existing
        return cluster.Network(routes=(), stops=())

    monkeypatch.setattr(cluster, "run", fake_cluster)
    monkeypatch.setattr(gtfs, "export", lambda engine=None: called.setdefault("feed", tmp_path))

    process.run_all(ENGINE, replace_existing=True)

    assert called["replace"] is True
    assert called["feed"] == tmp_path


def test_run_all_survives_a_network_too_small_to_export(monkeypatch, caplog):
    """Early on there may be no route with two stops yet. The matching still stands."""
    import cluster
    import gtfs

    done = process.ProcessReport(
        outcomes=(process.TraceOutcome(trace_id=1, status=process.STATUS_MATCHED),)
    )
    monkeypatch.setattr(process, "run", lambda engine=None, **kwargs: done)
    monkeypatch.setattr(cluster, "run", lambda **k: cluster.Network(routes=(), stops=()))

    def empty(engine=None):
        raise gtfs.EmptyFeedError("No route has two stops.")

    monkeypatch.setattr(gtfs, "export", empty)

    with caplog.at_level(logging.WARNING):
        report = process.run_all(ENGINE)

    assert report.matched == 1
    assert "No feed written" in caplog.text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_main_passes_its_flags_through(monkeypatch, capsys):
    seen: dict = {}

    def spy(engine=None, **kwargs):
        seen.update(kwargs)
        return process.ProcessReport()

    monkeypatch.setattr(process, "run", spy)

    assert process.main(["--retry", "--limit", "3", "--min-matched-fraction", "0.8"]) == 0
    assert seen["retry"] is True
    assert seen["limit"] == 3
    assert seen["min_matched_fraction"] == pytest.approx(0.8)
    assert seen["dry_run"] is False


def test_main_collects_repeated_trace_flags(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        process, "run", lambda engine=None, **kwargs: seen.update(kwargs) or process.ProcessReport()
    )

    process.main(["--trace", "1", "--trace", "2"])

    assert seen["trace_ids"] == [1, 2]


def test_main_routes_full_runs_through_run_all(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        process,
        "run_all",
        lambda engine=None, **kwargs: seen.update(kwargs) or process.ProcessReport(),
    )
    monkeypatch.setattr(process, "run", lambda *a, **k: pytest.fail("should have used run_all"))

    process.main(["--full", "--replace"])

    assert seen["replace_existing"] is True


def test_main_reports_a_non_zero_exit_when_the_run_aborted(monkeypatch, capsys):
    monkeypatch.setattr(
        process,
        "run",
        lambda engine=None, **kwargs: process.ProcessReport(aborted="Connection refused."),
    )

    assert process.main([]) == 1


def test_main_prints_the_traces_that_did_not_match(monkeypatch, capsys):
    report = process.ProcessReport(
        outcomes=(
            process.TraceOutcome(trace_id=1, status=process.STATUS_MATCHED),
            process.TraceOutcome(trace_id=2, status=process.STATUS_REJECTED, reason="too little"),
        )
    )
    monkeypatch.setattr(process, "run", lambda engine=None, **kwargs: report)

    process.main([])
    printed = capsys.readouterr().out

    assert "trace 2: rejected — too little" in printed
    assert "trace 1" not in printed, "a matched trace needs no line of its own"


@pytest.mark.parametrize("argv", [["--replace"], ["--dry-run", "--full"]])
def test_main_rejects_contradictory_flags(argv):
    with pytest.raises(SystemExit) as raised:
        process.main(argv)
    assert raised.value.code == 2


def test_main_rejects_two_selections_at_once():
    with pytest.raises(SystemExit):
        process.main(["--all", "--retry"])
