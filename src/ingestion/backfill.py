"""One-time back-catalog pull. For each tracked channel: resolve it, page
through its entire uploads list, fetch metadata + current metrics for every
video, and store them. Each video gets one snapshot tagged 'backfill' — the
starting point of its time series. Run this once per channel; re-running is
safe (everything upserts) and simply refreshes current metrics.

    python -m src.ingestion.backfill
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from src.config import settings
from src.db.session import SessionLocal
from src.ingestion.store import insert_snapshots, upsert_channel, upsert_videos
from src.ingestion.youtube_client import QuotaExceeded, YouTubeClient

# sub-niche tagging keeps pattern analysis clean (agency-building vs AI-tools)
NICHE_MAP = {
    "@nicksaraev": "ai-agency",
    "@LiamOttley": "ai-agency",
    "@vaibhavsisinty": "ai-tools",
}


def run() -> None:
    session = SessionLocal()
    run_id = session.execute(
        text("INSERT INTO pipeline_runs (trigger, status) "
             "VALUES ('manual-backfill', 'running') RETURNING id")
    ).scalar()
    session.commit()

    seen_videos = 0
    seen_snaps = 0
    try:
        with YouTubeClient(api_key=settings.youtube_api_key) as yt:
            for handle in settings.channel_handles:
                ch = yt.get_channel(handle)
                if not ch:
                    print(f"  !! could not resolve {handle}, skipping")
                    continue
                niche = NICHE_MAP.get(handle, "ai-automation")
                upsert_channel(session, ch, niche=niche)
                session.commit()
                print(f"  channel: {ch['title']} ({ch['video_count']} videos)")

                video_ids = list(yt.iter_video_ids(ch["uploads_playlist"]))
                print(f"    collected {len(video_ids)} video ids")

                videos = yt.get_videos(video_ids)
                captured = datetime.now(timezone.utc)
                upsert_videos(session, videos)
                snaps = insert_snapshots(session, videos, captured, source="backfill")
                session.commit()
                seen_videos += len(videos)
                seen_snaps += snaps
                print(f"    stored {len(videos)} videos + {snaps} snapshots "
                      f"(quota used: {yt.quota_used})")

        session.execute(
            text("UPDATE pipeline_runs SET status='ok', finished_at=now(), "
                 "stage_counts=:c WHERE id=:i"),
            {"c": f'{{"videos": {seen_videos}, "snapshots": {seen_snaps}}}',
             "i": run_id},
        )
        session.commit()
        print(f"done. {seen_videos} videos, {seen_snaps} snapshots.")
    except QuotaExceeded as e:
        session.execute(
            text("UPDATE pipeline_runs SET status='error', finished_at=now(), "
                 "error=:e WHERE id=:i"),
            {"e": f"quota: {e}", "i": run_id},
        )
        session.commit()
        print(f"stopped on quota: {e} (partial data saved)")
    except Exception as e:
        session.execute(
            text("UPDATE pipeline_runs SET status='error', finished_at=now(), "
                 "error=:e WHERE id=:i"),
            {"e": str(e)[:500], "i": run_id},
        )
        session.commit()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    run()
