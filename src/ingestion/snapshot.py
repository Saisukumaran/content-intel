"""The repeating velocity poll — this is what the cron runs on a schedule.
It re-measures every video we already track and appends a fresh snapshot
tagged 'live'. The difference between consecutive snapshots is velocity; that
growing history is what makes the system a time-series, not a single reading.

    python -m src.ingestion.snapshot
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from src.config import settings
from src.db.session import SessionLocal
from src.ingestion.store import insert_snapshots, tracked_video_ids, upsert_videos
from src.ingestion.youtube_client import QuotaExceeded, YouTubeClient


def run(trigger: str = "cron") -> None:
    session = SessionLocal()
    run_id = session.execute(
        text("INSERT INTO pipeline_runs (trigger, status) "
             "VALUES (:t, 'running') RETURNING id"),
        {"t": trigger},
    ).scalar()
    session.commit()

    try:
        ids = tracked_video_ids(session)
        if not ids:
            print("no tracked videos yet — run backfill first.")
            session.execute(
                text("UPDATE pipeline_runs SET status='ok', finished_at=now(), "
                     "stage_counts='{\"snapshots\": 0}' WHERE id=:i"),
                {"i": run_id})
            session.commit()
            return

        with YouTubeClient(api_key=settings.youtube_api_key) as yt:
            videos = yt.get_videos(ids)
            captured = datetime.now(timezone.utc)
            upsert_videos(session, videos)              # refresh title/thumb if changed
            snaps = insert_snapshots(session, videos, captured, source="live")
            session.commit()
            quota = yt.quota_used

        session.execute(
            text("UPDATE pipeline_runs SET status='ok', finished_at=now(), "
                 "stage_counts=:c WHERE id=:i"),
            {"c": f'{{"snapshots": {snaps}}}', "i": run_id})
        session.commit()
        print(f"snapshot run: {snaps} rows at {captured:%Y-%m-%d %H:%M} "
              f"(quota used: {quota})")
    except QuotaExceeded as e:
        session.execute(
            text("UPDATE pipeline_runs SET status='error', finished_at=now(), "
                 "error=:e WHERE id=:i"), {"e": f"quota: {e}", "i": run_id})
        session.commit()
        print(f"stopped on quota: {e}")
    except Exception as e:
        session.execute(
            text("UPDATE pipeline_runs SET status='error', finished_at=now(), "
                 "error=:e WHERE id=:i"), {"e": str(e)[:500], "i": run_id})
        session.commit()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    run(trigger="manual")
