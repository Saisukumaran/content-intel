"""Ingestion tests. These prove the contract without hitting the live API:
parsing, pagination, idempotent storage, and that consecutive snapshots yield
a correct velocity. The API is replaced with fixture responses; the database
work runs against a real Postgres (DATABASE_URL), so the SQL is genuinely
exercised, not mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.db.session import SessionLocal, engine
from src.ingestion import store
from src.ingestion.youtube_client import YouTubeClient, parse_duration

TEST_CH = "UC_test_ingest"


# ---- pure unit: ISO-8601 duration parsing --------------------------------
def test_parse_duration():
    assert parse_duration("PT1H2M10S") == 3730
    assert parse_duration("PT15M") == 900
    assert parse_duration("PT45S") == 45
    assert parse_duration("P0D") is None or parse_duration("P0D") == 0
    assert parse_duration(None) is None


# ---- client parsing + pagination (API faked) -----------------------------
def test_client_parses_and_paginates(monkeypatch):
    CHANNEL = {"items": [{
        "id": TEST_CH,
        "snippet": {"title": "Test Channel"},
        "statistics": {"subscriberCount": "1000", "videoCount": "3", "viewCount": "50000"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU_test"}},
    }]}
    PAGE1 = {"items": [{"contentDetails": {"videoId": "v1"}},
                       {"contentDetails": {"videoId": "v2"}}],
             "nextPageToken": "TOK"}
    PAGE2 = {"items": [{"contentDetails": {"videoId": "v3"}}]}
    VIDEOS = {"items": [{
        "id": "v1",
        "snippet": {"title": "I Made $12,000 with AI", "channelId": TEST_CH,
                     "publishedAt": "2026-06-20T10:00:00Z", "categoryId": "27",
                     "tags": ["ai", "automation"],
                     "thumbnails": {"high": {"url": "http://t/v1.jpg"}}},
        "statistics": {"viewCount": "8000", "likeCount": "400", "commentCount": "50"},
        "contentDetails": {"duration": "PT12M30S", "caption": "true"},
    }]}

    def fake_get(self, endpoint, params, cost=1):
        self.quota_used += cost
        if endpoint == "channels":
            return CHANNEL
        if endpoint == "playlistItems":
            return PAGE2 if params.get("pageToken") == "TOK" else PAGE1
        if endpoint == "videos":
            return VIDEOS
        raise AssertionError(endpoint)

    monkeypatch.setattr(YouTubeClient, "_get", fake_get)

    with YouTubeClient(api_key="fake") as yt:
        ch = yt.get_channel("@test")
        assert ch["channel_id"] == TEST_CH
        assert ch["uploads_playlist"] == "UU_test"

        ids = list(yt.iter_video_ids("UU_test"))
        assert ids == ["v1", "v2", "v3"]          # pagination consumed both pages

        vids = yt.get_videos(["v1"])
        v = vids[0]
        assert v["duration_seconds"] == 750        # 12m30s parsed
        assert v["captions_available"] is True
        assert v["view_count"] == 8000
        assert v["tags"] == ["ai", "automation"]


# ---- storage + dedup + velocity (real DB) --------------------------------
@pytest.fixture
def clean_db():
    """Remove any leftover test rows before and after."""
    def _wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM metric_snapshots WHERE video_id LIKE 'tv_%'"))
            c.execute(text("DELETE FROM videos WHERE channel_id = :c"), {"c": TEST_CH})
            c.execute(text("DELETE FROM channels WHERE channel_id = :c"), {"c": TEST_CH})
    _wipe()
    yield
    _wipe()


def test_storage_dedup_and_velocity(clean_db):
    session = SessionLocal()
    store.upsert_channel(session, {
        "channel_id": TEST_CH, "handle": "@test", "title": "Test",
        "subscriber_count": 1000, "video_count": 1, "view_count": 50000,
    })
    videos = [{
        "video_id": "tv_1", "channel_id": TEST_CH, "title": "v",
        "description": None, "published_at": "2026-06-20T10:00:00Z",
        "duration_seconds": 600, "category_id": "27", "default_language": "en",
        "thumbnail_url": "http://t.jpg", "tags": None, "captions_available": True,
        "view_count": 1000, "like_count": 50, "comment_count": 5,
    }]
    store.upsert_videos(session, videos)

    t0 = datetime.now(timezone.utc) - timedelta(hours=6)
    t1 = datetime.now(timezone.utc)
    store.insert_snapshots(session, videos, t0, "backfill")

    videos[0]["view_count"] = 4000        # video gained 3000 views over 6 hours
    store.insert_snapshots(session, videos, t1, "live")
    # dedup: re-inserting the same captured_at must NOT create a duplicate
    store.insert_snapshots(session, videos, t1, "live")
    session.commit()

    # exactly two snapshot rows survived (dedup worked)
    n = session.execute(
        text("SELECT count(*) FROM metric_snapshots WHERE video_id='tv_1'")
    ).scalar()
    assert n == 2

    # velocity query: views gained per hour between consecutive snapshots
    vph = session.execute(text("""
        SELECT ROUND(
            (view_count - LAG(view_count) OVER (ORDER BY captured_at))
            / (EXTRACT(EPOCH FROM (captured_at -
                 LAG(captured_at) OVER (ORDER BY captured_at)))/3600.0)
        )
        FROM metric_snapshots WHERE video_id='tv_1' ORDER BY captured_at
    """)).fetchall()
    # second row = 3000 views / 6 hours = 500 views/hour
    assert vph[1][0] == 500
    session.close()
