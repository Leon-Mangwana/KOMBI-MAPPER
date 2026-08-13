"""Driving uploaded traces through cleaning and map matching.

`clean.py`, `match.py` and `stops.py` are libraries: each takes a frame and
gives one back, and none of them decides *which* traces to work on. This module
is the batch job that walks the `traces` table and puts every new upload through
them. It is what turns those libraries into a stage that runs.

Where it sits
-------------
`api` writes a trace with `status = 'uploaded'` and no `matched_geom`. This
stage reads those, and writes `matched_geom` plus a terminal status.
`cluster.py` then reads every trace that *has* a `matched_geom` — so whether a
trace is written here is exactly what decides whether it can become a route.

Statuses
--------
A trace ends in one of three states, and the difference matters when you come
back to a run and ask what happened:

- `matched` — cleaned, matched, geometry stored. Clustering will see it.
- `rejected` — the run worked, but OSRM matched too little of the trace to
  trust it (see `MIN_MATCHED_FRACTION`). No geometry is stored, so clustering
  skips it. Nothing is wrong with the database; the recording was poor.
- `failed` — this trace could not be processed at all: no points, no
  timestamps, too few fixes surviving the accuracy gates.

The quality gate
----------------
`match.match_trace` never fails a trace outright — a stretch OSRM cannot match
falls back to cleaned GPS geometry and is flagged on the result. That is the
right behaviour for a library, and the wrong thing to feed to clustering
unchecked: a trace that came back 30% matched is mostly raw GPS noise wearing a
matched trace's clothes, and the Fréchet distances it produces would be
meaningless. So the gate lives here, and a trace below it is stored nowhere
rather than stored badly — `matched_geom` is the only signal clustering reads,
and leaving it set would silently let a rejected trace back in.

OSRM being down is not a bad trace
----------------------------------
`OSRMError` means the matcher was unreachable or answered nonsense, which says
nothing about the trace in hand. Marking traces `failed` on it would burn
through the whole backlog in seconds and leave a table full of traces that are
perfectly fine — and, worse, ones this stage would then never look at again. So
an `OSRMError` aborts the run: everything already processed keeps its status,
everything untouched stays `uploaded`, and a re-run after OSRM is back picks up
exactly where this one stopped.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, bindparam, text

from clean import clean_trace, load_trace
from config import get_engine
from match import MatchResult, OSRMError, match_trace, store_matched_geom

logger = logging.getLogger(__name__)

#: The status the API writes on upload, and so the one this stage looks for.
STATUS_UPLOADED = "uploaded"

#: Terminal statuses. See the module docstring.
STATUS_MATCHED = "matched"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

#: Share of a trace's length OSRM must actually have matched for the result to
#: be stored. Below this the trace is `rejected`.
#:
#: 0.6 is deliberately lenient. Harare's informal network runs partly on roads
#: OSM has thinly mapped, and a trace that is genuinely on a road OSRM does not
#: know about will fall back for that stretch through no fault of the recording.
#: The gate is here to catch traces that are mostly fallback — a walk, a ride in
#: a private car through a compound, a phone that spent the trip in a bag — not
#: to hold out for a perfect match.
MIN_MATCHED_FRACTION = 0.6

#: Fewer surviving fixes than this and there is nothing to match: two points
#: make a straight line between them, which OSRM will happily snap to a road
#: that the kombi may never have driven.
MIN_POINTS = 5


@dataclass(frozen=True)
class TraceOutcome:
    """What happened to one trace.

    Attributes:
        trace_id: The trace.
        status: One of the terminal statuses above.
        matched_fraction: Share of the output OSRM matched, or `None` if
            matching never ran.
        length_m: Length of the matched line, or `None` as above.
        points: Fixes that survived cleaning, or `None` if loading failed.
        reason: Why a trace was rejected or failed. `None` when matched.
    """

    trace_id: int
    status: str
    matched_fraction: float | None = None
    length_m: float | None = None
    points: int | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_MATCHED


@dataclass(frozen=True)
class ProcessReport:
    """The result of one batch.

    `aborted` carries the reason the run stopped early — an unreachable OSRM —
    and is `None` for a run that got through its whole list. The outcomes
    already recorded are kept either way, so a partial run still reports what it
    managed to do.
    """

    outcomes: tuple[TraceOutcome, ...] = ()
    aborted: str | None = None

    def _count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)

    @property
    def matched(self) -> int:
        return self._count(STATUS_MATCHED)

    @property
    def rejected(self) -> int:
        return self._count(STATUS_REJECTED)

    @property
    def failed(self) -> int:
        return self._count(STATUS_FAILED)

    @property
    def matched_ids(self) -> list[int]:
        return [outcome.trace_id for outcome in self.outcomes if outcome.ok]

    def summary(self) -> str:
        text_ = (
            f"{len(self.outcomes)} trace(s) processed: "
            f"{self.matched} matched, {self.rejected} rejected, {self.failed} failed"
        )
        return f"{text_} (aborted: {self.aborted})" if self.aborted else text_


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

_PENDING_SQL = text(
    """
    SELECT id
    FROM traces
    WHERE status IN :statuses
    ORDER BY id
    """
).bindparams(bindparam("statuses", expanding=True))

_ALL_TRACES_SQL = text("SELECT id FROM traces ORDER BY id")

_UPDATE_STATUS_SQL = text("UPDATE traces SET status = :status WHERE id = :trace_id")

_CLEAR_MATCHED_GEOM_SQL = text("UPDATE traces SET matched_geom = NULL WHERE id = :trace_id")


def pending_trace_ids(
    engine: Engine | None = None,
    *,
    statuses: tuple[str, ...] = (STATUS_UPLOADED,),
    limit: int | None = None,
) -> list[int]:
    """Ids of the traces waiting for this stage, oldest first.

    Selection is on `status`, not on `matched_geom IS NULL`: the status is this
    stage's own record of having looked at a trace, and a trace it rejected has
    no geometry either. Going by geometry alone would mean re-matching every
    rejected trace on every run, forever.

    Args:
        engine: Database engine. Defaults to the process-wide one.
        statuses: Which statuses count as pending. Widen it to re-run traces a
            previous batch rejected or failed.
        limit: Take at most this many. Useful for a first cautious run.
    """
    engine = engine if engine is not None else get_engine()
    with engine.connect() as conn:
        rows = conn.execute(_PENDING_SQL, {"statuses": list(statuses)}).scalars().all()

    ids = [int(value) for value in rows]
    return ids if limit is None else ids[:limit]


def all_trace_ids(engine: Engine | None = None, *, limit: int | None = None) -> list[int]:
    """Every trace id, oldest first, whatever its status."""
    engine = engine if engine is not None else get_engine()
    with engine.connect() as conn:
        rows = conn.execute(_ALL_TRACES_SQL).scalars().all()

    ids = [int(value) for value in rows]
    return ids if limit is None else ids[:limit]


def set_status(trace_id: int, status: str, engine: Engine | None = None) -> None:
    """Write a trace's status. Raises `LookupError` if it does not exist."""
    engine = engine if engine is not None else get_engine()
    with engine.begin() as conn:
        updated = conn.execute(_UPDATE_STATUS_SQL, {"trace_id": trace_id, "status": status})
        if updated.rowcount == 0:
            raise LookupError(f"Trace {trace_id} does not exist.")


def clear_matched_geom(trace_id: int, engine: Engine | None = None) -> None:
    """Drop a trace's matched geometry.

    Called when a re-run rejects a trace an earlier run had stored. Without it
    the old geometry would stay behind and clustering would keep reading a line
    this stage has since decided it does not trust.
    """
    engine = engine if engine is not None else get_engine()
    with engine.begin() as conn:
        conn.execute(_CLEAR_MATCHED_GEOM_SQL, {"trace_id": trace_id})


# --------------------------------------------------------------------------
# One trace
# --------------------------------------------------------------------------


def classify(result: MatchResult, *, min_matched_fraction: float = MIN_MATCHED_FRACTION) -> str:
    """`STATUS_MATCHED` if enough of the trace matched, else `STATUS_REJECTED`."""
    return STATUS_MATCHED if result.matched_fraction >= min_matched_fraction else STATUS_REJECTED


def process_trace(
    trace_id: int,
    engine: Engine | None = None,
    *,
    min_matched_fraction: float = MIN_MATCHED_FRACTION,
    min_points: int = MIN_POINTS,
    dry_run: bool = False,
    **match_kwargs: Any,
) -> TraceOutcome:
    """Clean, match, gate and store one trace. Returns what happened to it.

    Errors that are specific to this trace — no points, no timestamps, nothing
    left after the accuracy gates — become a `failed` outcome rather than an
    exception, so a batch survives one bad upload. `OSRMError` is *not* caught
    here; see the module docstring.

    Args:
        trace_id: The trace to process.
        engine: Database engine. Defaults to the process-wide one.
        min_matched_fraction: The quality gate. See `MIN_MATCHED_FRACTION`.
        min_points: Fixes that must survive cleaning. See `MIN_POINTS`.
        dry_run: Do everything except write. The outcome is what a real run
            would have recorded.
        **match_kwargs: Forwarded to `match.match_trace`.
    """
    engine = engine if engine is not None else get_engine()

    try:
        cleaned = clean_trace(load_trace(trace_id, engine=engine))
    except (LookupError, ValueError) as error:
        # ValueError is what clean.py raises for a trace with missing or
        # unusable timestamps, which is a property of the upload, not a bug.
        return _fail(trace_id, str(error), engine=engine, dry_run=dry_run)

    if len(cleaned) < min_points:
        return _fail(
            trace_id,
            f"only {len(cleaned)} fix(es) survived cleaning; {min_points} needed",
            engine=engine,
            dry_run=dry_run,
            points=len(cleaned),
        )

    result = match_trace(cleaned, **match_kwargs)
    status = classify(result, min_matched_fraction=min_matched_fraction)
    reason = (
        None
        if status == STATUS_MATCHED
        else (
            f"OSRM matched {result.matched_fraction:.0%} of the line, "
            f"under the {min_matched_fraction:.0%} gate"
        )
    )

    if not dry_run:
        if status == STATUS_MATCHED:
            store_matched_geom(trace_id, result, engine=engine)
        else:
            clear_matched_geom(trace_id, engine=engine)
        set_status(trace_id, status, engine=engine)

    logger.info(
        "Trace %d: %s (%.0f%% matched, %.1f km, %d fix(es)).",
        trace_id,
        status,
        result.matched_fraction * 100.0,
        result.length_m / 1000.0,
        len(cleaned),
    )
    return TraceOutcome(
        trace_id=trace_id,
        status=status,
        matched_fraction=result.matched_fraction,
        length_m=result.length_m,
        points=len(cleaned),
        reason=reason,
    )


def _fail(
    trace_id: int,
    reason: str,
    *,
    engine: Engine | None,
    dry_run: bool,
    points: int | None = None,
) -> TraceOutcome:
    """Record a per-trace failure and return the outcome."""
    logger.warning("Trace %d failed: %s", trace_id, reason)
    if not dry_run:
        set_status(trace_id, STATUS_FAILED, engine=engine)
    return TraceOutcome(
        trace_id=trace_id, status=STATUS_FAILED, points=points, reason=reason
    )


# --------------------------------------------------------------------------
# A batch
# --------------------------------------------------------------------------


def process_traces(
    trace_ids: list[int],
    engine: Engine | None = None,
    **process_kwargs: Any,
) -> ProcessReport:
    """Process each trace in turn. Stops early if OSRM goes away.

    Keyword arguments are forwarded to `process_trace`.
    """
    engine = engine if engine is not None else get_engine()

    outcomes: list[TraceOutcome] = []
    for trace_id in trace_ids:
        try:
            outcomes.append(process_trace(trace_id, engine=engine, **process_kwargs))
        except OSRMError as error:
            logger.error(
                "OSRM is not answering; stopping after %d of %d trace(s). "
                "The rest keep their current status and a re-run will pick them up. (%s)",
                len(outcomes),
                len(trace_ids),
                error,
            )
            return ProcessReport(outcomes=tuple(outcomes), aborted=str(error))

    return ProcessReport(outcomes=tuple(outcomes))


def run(
    engine: Engine | None = None,
    *,
    trace_ids: list[int] | None = None,
    reprocess: bool = False,
    retry: bool = False,
    limit: int | None = None,
    **process_kwargs: Any,
) -> ProcessReport:
    """Pick the traces to work on and process them. The entry point.

    Args:
        engine: Database engine. Defaults to the process-wide one.
        trace_ids: Process exactly these, ignoring status. `None` selects.
        reprocess: Process every trace in the table, whatever its status.
        retry: Also take traces a previous run rejected or failed.
        limit: Take at most this many.
        **process_kwargs: Forwarded to `process_trace`.
    """
    engine = engine if engine is not None else get_engine()

    if trace_ids is None:
        if reprocess:
            trace_ids = all_trace_ids(engine, limit=limit)
        else:
            statuses = (STATUS_UPLOADED,)
            if retry:
                statuses = (STATUS_UPLOADED, STATUS_REJECTED, STATUS_FAILED)
            trace_ids = pending_trace_ids(engine, statuses=statuses, limit=limit)

    if not trace_ids:
        logger.info("No traces to process.")
        return ProcessReport()

    logger.info("Processing %d trace(s).", len(trace_ids))
    report = process_traces(trace_ids, engine=engine, **process_kwargs)
    logger.info("Done: %s.", report.summary())
    return report


def run_all(
    engine: Engine | None = None,
    *,
    replace_existing: bool = False,
    **run_kwargs: Any,
) -> ProcessReport:
    """Match everything pending, then re-cluster and re-export the feed.

    The whole backend in one call: stages 3 and 4 of the pipeline in the README.
    Clustering is global — it looks at every matched trace, not just this
    batch's — so it runs once at the end rather than per trace.

    Clustering and export are skipped when the run matched nothing new *and*
    was not asked to rebuild, since re-clustering identical inputs would spend
    minutes to write the same routes back.

    `cluster` and `gtfs` are imported here rather than at module scope: they
    pull in scikit-learn and the feed builder, and the common case — matching a
    handful of new uploads — needs neither.
    """
    engine = engine if engine is not None else get_engine()
    report = run(engine, **run_kwargs)

    if not report.matched_ids and not replace_existing:
        logger.info("Nothing newly matched; leaving the existing routes and feed alone.")
        return report

    if report.aborted:
        # Clustering what did get through would produce a network built from an
        # arbitrary prefix of the backlog, and then publish it as a feed.
        logger.error("Run aborted before the backlog was finished; not re-clustering.")
        return report

    # Imported here, not at module scope — see the docstring.
    import cluster
    import gtfs

    network = cluster.run(engine=engine, replace_existing=replace_existing)
    logger.info("Clustered %s.", network.summary())

    try:
        written = gtfs.export(engine=engine)
        logger.info("Exported %s.", written)
    except gtfs.EmptyFeedError as error:
        # A network with no route carrying two stops is a real state early on,
        # when there are only a few traces. The matching above still stands.
        logger.warning("No feed written: %s", error)

    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--trace",
        type=int,
        action="append",
        dest="trace_ids",
        metavar="ID",
        help="process this trace whatever its status; repeatable",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        dest="reprocess",
        help="process every trace in the table, not only the pending ones",
    )
    selection.add_argument(
        "--retry",
        action="store_true",
        help="also process traces a previous run rejected or failed",
    )
    parser.add_argument("--limit", type=int, default=None, help="process at most this many traces")
    parser.add_argument(
        "--min-matched-fraction",
        type=float,
        default=MIN_MATCHED_FRACTION,
        help=f"quality gate, 0-1 (default {MIN_MATCHED_FRACTION:.2f})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="clean and match, but write nothing back",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="after matching, re-cluster the network and re-export the GTFS feed",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="with --full, rebuild the routes and stops from scratch (see cluster.persist_network)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.replace and not args.full:
        parser.error("--replace only means anything with --full")
    if args.dry_run and args.full:
        parser.error("--dry-run writes nothing, so there is nothing for --full to cluster")

    options: dict[str, Any] = {
        "trace_ids": args.trace_ids,
        "reprocess": args.reprocess,
        "retry": args.retry,
        "limit": args.limit,
        "min_matched_fraction": args.min_matched_fraction,
        "dry_run": args.dry_run,
    }

    if args.full:
        report = run_all(replace_existing=args.replace, **options)
    else:
        report = run(**options)

    print(report.summary())
    for outcome in report.outcomes:
        if outcome.ok:
            continue
        print(f"  trace {outcome.trace_id}: {outcome.status} — {outcome.reason}")

    return 1 if report.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
