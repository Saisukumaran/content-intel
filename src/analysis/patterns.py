"""Surfaces *what's working* by grouping videos on each attribute and ranking by
average outlier score — segmented by sub-niche so the agency lane and the
AI-tools lane never blur together. A group needs a minimum number of videos to
appear, so we never crown a pattern off a single lucky video.

This module is the analysis the recommender consumes. Run it directly to see
the patterns for a segment:

    python -m src.analysis.patterns                # default: ai-agency
    python -m src.analysis.patterns ai-tools
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from src.db.session import SessionLocal

# attributes we rank on, with the column that holds each
ATTRIBUTES = ["title_mechanic", "format", "topic", "hook_type",
              "duration_bucket", "publish_dow"]

_PATTERN_SQL = """
SELECT a.{attr} AS value,
       COUNT(*) AS n,
       ROUND(AVG(p.outlier_score), 2) AS avg_outlier,
       ROUND(MAX(p.outlier_score), 2) AS best
FROM video_attributes a
JOIN videos v   ON v.video_id = a.video_id
JOIN channels c ON c.channel_id = v.channel_id
JOIN video_performance p ON p.video_id = a.video_id
WHERE c.niche = :niche
  AND a.{attr} IS NOT NULL
  AND p.outlier_score IS NOT NULL
GROUP BY a.{attr}
HAVING COUNT(*) >= :min_n
ORDER BY avg_outlier DESC;
"""


def what_works(session, niche: str, attr: str, min_n: int = 3) -> list[dict]:
    if attr not in ATTRIBUTES:
        raise ValueError(f"unknown attribute {attr}")
    rows = session.execute(
        text(_PATTERN_SQL.format(attr=attr)), {"niche": niche, "min_n": min_n}
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def summary(niche: str = "ai-agency", min_n: int = 3) -> dict[str, list[dict]]:
    session = SessionLocal()
    out = {attr: what_works(session, niche, attr, min_n) for attr in ATTRIBUTES}
    session.close()
    return out


_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_value(attr: str, value) -> str:
    if attr == "publish_dow" and value is not None:
        return _DOW[int(value)]
    return str(value)


if __name__ == "__main__":
    niche = sys.argv[1] if len(sys.argv) > 1 else "ai-agency"
    data = summary(niche)
    print(f"\nWHAT'S WORKING — segment: {niche}\n" + "=" * 46)
    for attr, rows in data.items():
        if not rows:
            continue
        print(f"\n{attr}:")
        for r in rows[:6]:
            print(f"  {_fmt_value(attr, r['value']):<16} "
                  f"avg outlier {r['avg_outlier']:>5}   "
                  f"(n={r['n']}, best={r['best']})")
    print()
