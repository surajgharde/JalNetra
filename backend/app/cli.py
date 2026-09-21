"""Operator CLI (S12).

    python -m app.cli backfill --water-body wb_khadakwasla --from 2023-01-01 --to 2026-09-01
    python -m app.cli backfill --water-body wb_khadakwasla --from 2023-01-01 --to 2026-09-01 \
        --resume
    python -m app.cli backfill-status --water-body wb_khadakwasla
    python -m app.cli ops-gauges

``backfill`` runs the whole chain in-process, one date chunk at a time -
ingest -> mask -> indicators -> anomalies -> scoring -> alerts - and writes a
checkpoint (the last completed chunk) to a ``jobs`` row after every chunk.
Killed half-way, ``--resume`` picks the newest checkpoint for the same body
and window and carries on from the next chunk; nothing already done is redone
because every stage is idempotent on (water body, scene). Rainfall for the
window is pulled first; baselines are rebuilt once at the end. Alerts created
by a backfill are never dispatched (history should not page anyone).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.storage import get_store
from app.db.models import Job, WaterBody
from app.db.sync_session import sync_session
from app.services.l03_ingestion.service import ingest_water_body
from app.services.l05_water_detection.service import process_water_body as process_masks
from app.services.l06_indicators.service import process_water_body as process_indicators
from app.services.l07_baseline.backfill import plan_backfill
from app.services.l07_baseline.rainfall import sync_rainfall
from app.services.l07_baseline.service import build_water_body_baselines, refresh_weekly
from app.services.l08_anomaly.service import process_water_body as process_anomalies
from app.services.l09_fusion.service import anomaly_scenes
from app.services.l09_fusion.service import process_water_body as process_scores
from app.services.l11_alerts.assembler import assemble_scene
from app.services.ops.gauges import refresh_ops_gauges

log = logging.getLogger("jalnetra.cli")


# --- checkpoints ----------------------------------------------------------------


def find_resumable(
    session: Session, water_body_id: str, date_from: date, date_to: date
) -> Job | None:
    return session.execute(
        select(Job)
        .where(
            Job.kind == "backfill-cli",
            Job.water_body_id == water_body_id,
            Job.date_from == date_from,
            Job.date_to == date_to,
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def checkpoint(
    session: Any, job: Job, *, completed_through: date, chunks_done: int, chunks_total: int
) -> None:
    job.status = "running"
    job.snapshot = {
        **(job.snapshot or {}),
        "completed_through": completed_through.isoformat(),
        "chunks_done": chunks_done,
        "chunks_total": chunks_total,
        "progress_pct": round(100.0 * chunks_done / chunks_total, 1) if chunks_total else 100.0,
        "current_stage": "backfill",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    session.commit()


# --- backfill --------------------------------------------------------------------


def run_chunk(
    session: Any, store: Any, wb: WaterBody, start: date, end: date, *, force: bool
) -> dict[str, int]:
    """Every stage for one chunk. Each stage is idempotent, so a resumed chunk
    only does what an earlier run left unfinished."""
    ingest = ingest_water_body(session, store, wb.id, start, date_to=end)
    session.commit()
    masks = process_masks(session, store, wb.id, start, end, force=force)
    session.commit()
    inds = process_indicators(session, store, wb.id, start, end, force=force)
    session.commit()
    anoms = process_anomalies(session, store, wb.id, start, end, force=force)
    session.commit()
    scores = process_scores(session, store, wb.id, start, end, force=force)
    session.commit()
    created = 0
    for scene in anomaly_scenes(session, wb.id, start, end):
        created += len(assemble_scene(session, wb, scene).created)
    session.commit()
    return {
        "scenes_found": ingest.scenes_found,
        "ingested": len(ingest.ingested),
        "unusable": len(ingest.unusable),
        "masked": len(masks.computed),
        "indicators": len(inds.computed),
        "anomalies": len(anoms.computed),
        "scored": len(scores.scored),
        "alerts_created": created,
    }


def cmd_backfill(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = get_store()
    date_from, date_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    plan = plan_backfill(args.water_body, date_from=date_from, date_to=date_to, settings=settings)
    with sync_session() as session:
        wb = session.get(WaterBody, args.water_body)
        if wb is None:
            print(f"unknown water body {args.water_body!r}", file=sys.stderr)
            return 2
        job = find_resumable(session, wb.id, plan.date_from, plan.date_to) if args.resume else None
        start_index = 0
        if job is not None:
            done_through = job.snapshot.get("completed_through")
            if done_through:
                d = date.fromisoformat(done_through)
                start_index = next(
                    (i for i, (s, _e) in enumerate(plan.chunks) if s > d), len(plan.chunks)
                )
            print(f"resuming job {job.id} from chunk {start_index + 1}/{plan.n_chunks}")
        else:
            job = Job(
                id=f"job_{int(time.time())}_{wb.id}",
                kind="backfill-cli",
                water_body_id=wb.id,
                date_from=plan.date_from,
                date_to=plan.date_to,
                status="running",
                requested_by=args.requested_by,
                snapshot={"chunks_total": plan.n_chunks, "chunks_done": 0},
            )
            session.add(job)
            session.commit()
            print(
                f"job {job.id}: {plan.n_chunks} chunks of "
                f"{settings.baseline_backfill_chunk_days} days"
            )

        if not args.skip_rainfall:
            r = sync_rainfall(session, wb.id, plan.date_from, plan.date_to, settings=settings)
            session.commit()
            print(f"rainfall: {r.archive_rows} archive rows, {r.forecast_rows} forecast rows")

        totals: dict[str, int] = {}
        try:
            for i in range(start_index, plan.n_chunks):
                start, end = plan.chunks[i]
                t0 = time.perf_counter()
                counts = run_chunk(session, store, wb, start, end, force=args.force)
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
                checkpoint(
                    session,
                    job,
                    completed_through=end,
                    chunks_done=i + 1,
                    chunks_total=plan.n_chunks,
                )
                secs = time.perf_counter() - t0
                print(f"[{i + 1}/{plan.n_chunks}] {start} -> {end}: {counts} ({secs:.0f}s)")
        except KeyboardInterrupt:
            session.rollback()
            print(f"\ninterrupted; re-run with --resume to continue job {job.id}")
            return 130
        except Exception as exc:
            session.rollback()
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
            log.exception("backfill failed")
            print(f"failed: {job.error}; fix and re-run with --resume", file=sys.stderr)
            return 1

        if not args.skip_baselines:
            b = build_water_body_baselines(session, wb.id, settings=settings)
            session.commit()
            refresh_weekly(session)
            print(f"baselines: {b.series_built} series, {b.usable_windows} usable windows")
        job.status = "done"
        job.finished_at = datetime.now(UTC)
        job.snapshot = {
            **job.snapshot,
            "totals": totals,
            "progress_pct": 100.0,
            "current_stage": None,
        }
        session.commit()
        print(f"done: {totals}")
    return 0


def cmd_backfill_status(args: argparse.Namespace) -> int:
    with sync_session() as session:
        stmt = (
            select(Job).where(Job.kind == "backfill-cli").order_by(Job.created_at.desc()).limit(20)
        )
        if args.water_body:
            stmt = stmt.where(Job.water_body_id == args.water_body)
        for job in session.scalars(stmt).all():
            snap = job.snapshot or {}
            print(
                f"{job.id}  {job.water_body_id}  {job.date_from}..{job.date_to}  {job.status:8}"
                f"  {snap.get('chunks_done', 0)}/{snap.get('chunks_total', '?')} chunks"
                f"  through {snap.get('completed_through', '-')}"
                + (f"  error: {job.error}" if job.error else "")
            )
    return 0


def cmd_ops_gauges(_: argparse.Namespace) -> int:
    with sync_session() as session:
        snap = refresh_ops_gauges(session)
    for k, v in snap.items():
        print(f"{k}: {v}")
    return 0


# --- entrypoint ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jalnetra", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser(
        "backfill", help="run the full pipeline over a date window with resumable checkpoints"
    )
    b.add_argument("--water-body", required=True)
    b.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    b.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    b.add_argument(
        "--resume", action="store_true", help="continue the newest unfinished job for this window"
    )
    b.add_argument("--force", action="store_true", help="recompute stages that already have rows")
    b.add_argument("--skip-rainfall", action="store_true")
    b.add_argument("--skip-baselines", action="store_true")
    b.add_argument("--requested-by", default="cli")
    b.set_defaults(func=cmd_backfill)

    st = sub.add_parser(
        "backfill-status", help="list recent CLI backfill jobs and their checkpoints"
    )
    st.add_argument("--water-body")
    st.set_defaults(func=cmd_backfill_status)

    g = sub.add_parser("ops-gauges", help="recompute and print the operational gauges")
    g.set_defaults(func=cmd_ops_gauges)
    return p


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings().log_level)
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
