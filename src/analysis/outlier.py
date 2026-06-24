"""Computes each video's performance, normalised so channels are comparable.

The core metric is the **outlier score**: a video's latest views divided by its
own channel's *median* views. A score of 3.0 means "3x this channel's typical
video" — a real outlier regardless of whether the channel has 40k or 400k subs.
We use the median, not the mean, because a few viral videos would drag a mean
upward and make everything else look like an underperformer. (This is the
Rare Social methodology — normalise to the channel's own baseline.)

Where two or more snapshots exist for a video, we also compute recent velocity
(views/hour) — the time-series signal that a one-shot pull can't produce.

    python -m src.analysis.outlier
"""
from __future__ import annotations

from sqlalchemy import text

from src.db.session import SessionLocal

# latest snapshot per video, channel median, outlier score, lifetime views/day
_SCORE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (m.video_id)
        m.video_id, m.view_count, m.captured_at,
        v.channel_id, v.published_at
    FROM metric_snapshots m
    JOIN videos v ON v.video_id = m.video_id
    ORDER BY m.video_id, m.captured_at DESC
),
baseline AS (
    SELECT channel_id,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY view_count)::numeric AS median_views
    FROM latest
    WHERE view_count IS NOT NULL
    GROUP BY channel_id
)
INSERT INTO video_performance
    (video_id, as_of, latest_views, channel_baseline, outlier_score,
     views_per_day, velocity_recent, accelerating)
SELECT
    l.video_id,
    now(),
    l.view_count,
    b.median_views,
    CASE WHEN b.median_views > 0
         THEN ROUND(l.view_count::numeric / b.median_views, 3) END,
    CASE WHEN l.published_at IS NOT NULL
         THEN ROUND((l.view_count::numeric /
              GREATEST(EXTRACT(EPOCH FROM (now() - l.published_at))/86400.0, 1))::numeric, 1)
    END,
    NULL, NULL
FROM latest l
JOIN baseline b ON b.channel_id = l.channel_id
ON CONFLICT (video_id) DO UPDATE SET
    as_of = EXCLUDED.as_of,
    latest_views = EXCLUDED.latest_views,
    channel_baseline = EXCLUDED.channel_baseline,
    outlier_score = EXCLUDED.outlier_score,
    views_per_day = EXCLUDED.views_per_day;
"""

# recent velocity (views/hour) from the two most recent snapshots, where they exist
_VELOCITY_SQL = """
WITH ranked AS (
    SELECT video_id, view_count, captured_at,
           ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY captured_at DESC) AS rn
    FROM metric_snapshots
),
pairs AS (
    SELECT a.video_id,
           (a.view_count - b.view_count) AS dv,
           EXTRACT(EPOCH FROM (a.captured_at - b.captured_at))/3600.0 AS dh
    FROM ranked a JOIN ranked b
      ON a.video_id = b.video_id AND a.rn = 1 AND b.rn = 2
)
UPDATE video_performance p
SET velocity_recent = ROUND((pairs.dv / NULLIF(pairs.dh, 0))::numeric, 1)
FROM pairs
WHERE p.video_id = pairs.video_id AND pairs.dh > 0;
"""


def score_all() -> int:
    session = SessionLocal()
    session.execute(text(_SCORE_SQL))
    session.execute(text(_VELOCITY_SQL))
    session.commit()
    n = session.execute(text("SELECT count(*) FROM video_performance")).scalar()
    session.close()
    return n


if __name__ == "__main__":
    n = score_all()
    print(f"scored {n} videos (outlier + velocity where available)")
