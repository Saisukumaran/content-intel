"""The walkthrough centerpiece: prove the learning loop in 60 seconds.

It captures the current top recommendations, then injects a single new snapshot
that makes one chosen topic's videos surge — and re-runs scoring and the
recommender. The recommendations visibly re-rank because the underlying data
changed. This demonstrates the *mechanism* of learning without waiting days for
real data to accumulate.

Integrity: the injected snapshot is written with source='seeded', so it is
permanently distinguishable from real collected data in the database. We never
present seeded numbers as real. Pass --topic to choose which topic surges.

    python -m src.recommend.demo --topic n8n
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.analysis.outlier import score_all
from src.db.session import SessionLocal
from src.recommend.recommender import compare_last_two, run as recommend


def surge_topic(session, niche: str, topic: str, multiplier: float = 4.0) -> int:
    """Insert a seeded snapshot for each video of `topic` at multiplier x its
    latest views, timestamped just ahead of the last real snapshot so it becomes
    the newest reading. Returns how many videos were surged."""
    rows = session.execute(text("""
        SELECT DISTINCT ON (m.video_id) m.video_id, m.view_count,
               m.like_count, m.comment_count, m.captured_at
        FROM metric_snapshots m
        JOIN videos v ON v.video_id = m.video_id
        JOIN channels c ON c.channel_id = v.channel_id
        JOIN video_attributes a ON a.video_id = v.video_id
        WHERE c.niche = :niche AND a.topic = :topic
        ORDER BY m.video_id, m.captured_at DESC
    """), {"niche": niche, "topic": topic}).fetchall()

    ts = datetime.now(timezone.utc) + timedelta(seconds=1)
    for r in rows:
        session.execute(text("""
            INSERT INTO metric_snapshots
              (video_id, captured_at, view_count, like_count, comment_count, source)
            VALUES (:v, :t, :views, :likes, :comments, 'seeded')
            ON CONFLICT (video_id, captured_at) DO NOTHING
        """), {"v": r.video_id, "t": ts,
               "views": int((r.view_count or 0) * multiplier),
               "likes": r.like_count, "comments": r.comment_count})
    session.commit()
    return len(rows)


def main(topic: str = "n8n") -> None:
    print(f"\n=== LIVE LOOP DEMO: surging '{topic}' ===")
    print("\n--- recommendations BEFORE ---")
    recommend()                                  # snapshot current state (a run)

    session = SessionLocal()
    n = surge_topic(session, "ai-agency", topic)
    session.close()
    print(f"\n>>> injected {n} seeded snapshots ({topic} videos surged 4x) <<<")
    print(">>> (flagged source='seeded' in the DB — never shown as real data) <<<")

    print("\n--- rescoring + recommendations AFTER ---")
    score_all()
    recommend()                                  # re-derive from changed data

    compare_last_two()                           # show the before/after side by side


if __name__ == "__main__":
    topic = "n8n"
    if "--topic" in sys.argv:
        topic = sys.argv[sys.argv.index("--topic") + 1]
    main(topic)
