"""Writes ingested data to Postgres. Knows nothing about YouTube — it takes the
plain dicts the client returns and persists them. All writes are idempotent:
re-running ingestion never creates duplicate channels or videos (upsert), and
snapshot inserts skip exact collisions, so the pipeline is safe to run on a
schedule without manual cleanup.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.db.models import Channel, MetricSnapshot, Video


def upsert_channel(session: Session, ch: dict, niche: str = "ai-automation") -> None:
    stmt = pg_insert(Channel).values(
        channel_id=ch["channel_id"],
        handle=ch["handle"],
        title=ch["title"],
        subscriber_count=ch.get("subscriber_count"),
        video_count=ch.get("video_count"),
        view_count=ch.get("view_count"),
        niche=niche,
        stats_fetched_at=datetime.now(timezone.utc),
    )
    # on a repeat run, refresh the mutable stats but keep first_seen_at
    stmt = stmt.on_conflict_do_update(
        index_elements=["channel_id"],
        set_={
            "subscriber_count": stmt.excluded.subscriber_count,
            "video_count": stmt.excluded.video_count,
            "view_count": stmt.excluded.view_count,
            "stats_fetched_at": stmt.excluded.stats_fetched_at,
        },
    )
    session.execute(stmt)


def upsert_videos(session: Session, videos: list[dict]) -> int:
    """Insert/refresh the slowly-changing identity rows. Returns count seen."""
    for v in videos:
        stmt = pg_insert(Video).values(
            video_id=v["video_id"],
            channel_id=v["channel_id"],
            title=v["title"],
            description=v.get("description"),
            published_at=v["published_at"],
            duration_seconds=v.get("duration_seconds"),
            category_id=v.get("category_id"),
            default_language=v.get("default_language"),
            thumbnail_url=v.get("thumbnail_url"),
            tags=v.get("tags"),
            captions_available=v.get("captions_available"),
        )
        # title/thumbnail can change over a video's life; refresh them, keep the rest
        stmt = stmt.on_conflict_do_update(
            index_elements=["video_id"],
            set_={
                "title": stmt.excluded.title,
                "thumbnail_url": stmt.excluded.thumbnail_url,
            },
        )
        session.execute(stmt)
    return len(videos)


def insert_snapshots(
    session: Session, videos: list[dict], captured_at: datetime, source: str
) -> int:
    """Append one metric row per video. This is the time-series heartbeat.
    source is 'backfill' (one-time history pull) or 'live' (scheduled poll).
    Exact (video_id, captured_at) collisions are ignored so a double-run is safe.
    """
    rows = [
        {
            "video_id": v["video_id"],
            "captured_at": captured_at,
            "view_count": v.get("view_count"),
            "like_count": v.get("like_count"),
            "comment_count": v.get("comment_count"),
            "source": source,
        }
        for v in videos
    ]
    if not rows:
        return 0
    stmt = pg_insert(MetricSnapshot).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["video_id", "captured_at"])
    session.execute(stmt)
    return len(rows)


def tracked_video_ids(session: Session) -> list[str]:
    """Video ids we've already seen — the set the scheduled poll re-measures."""
    return [r[0] for r in session.execute(text("SELECT video_id FROM videos"))]
