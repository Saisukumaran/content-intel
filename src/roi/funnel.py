"""ROI as a transparent proxy funnel. We don't own the channels, so we have no
real conversion data — and pretending otherwise would be the fastest way to lose
a client's trust. Instead we instrument a clear, *stated* funnel and tie it to
the system's own recommendations:

    views -> link clicks -> site visits -> demo requests -> deals -> pipeline $

Two things make this credible rather than hand-wavy:

  1. Every conversion rate lives in `roi_assumptions`, versioned and editable. We
     show the assumptions next to the numbers; a client can dial them to their
     own reality and watch the model update. Nothing is hidden in code.

  2. The lift is grounded in *real observed data*. Baseline = the niche's median
     video. "Recommended" applies the actual outlier multiple we measure for the
     recommended attributes. So the ROI story is the same loop, monetised:
     we learned what outperforms, we recommend it, and here is the projected
     pipeline difference between following that advice and posting average
     content.

    python -m src.roi.funnel
"""
from __future__ import annotations

from sqlalchemy import text

from src.db.session import SessionLocal

# Conservative, clearly-labelled defaults. The point is not that these are
# precise — it's that they're explicit and a client can change them.
DEFAULT_ASSUMPTIONS = {
    "version": "v1",
    "view_to_click": 0.015,   # 1.5% of views click an off-platform CTA
    "click_to_visit": 0.75,   # 75% of clicks land on the site
    "visit_to_demo": 0.03,    # 3% of visitors request a demo
    "demo_to_deal": 0.25,     # 25% of demos close
    "avg_deal_value": 1500,   # $ per closed deal
    "note": "default v1 — conservative; tune to client's real funnel",
}


def seed_assumptions() -> None:
    """Insert the default assumptions row if none exists yet."""
    session = SessionLocal()
    exists = session.execute(text("SELECT count(*) FROM roi_assumptions")).scalar()
    if not exists:
        a = DEFAULT_ASSUMPTIONS
        session.execute(text("""
            INSERT INTO roi_assumptions
              (version, view_to_click, click_to_visit, visit_to_demo,
               demo_to_deal, avg_deal_value, note)
            VALUES (:version,:view_to_click,:click_to_visit,:visit_to_demo,
                    :demo_to_deal,:avg_deal_value,:note)
        """), a)
        session.commit()
    session.close()


def latest_assumptions(session) -> dict:
    r = session.execute(text("""
        SELECT version, view_to_click, click_to_visit, visit_to_demo,
               demo_to_deal, avg_deal_value
        FROM roi_assumptions ORDER BY id DESC LIMIT 1
    """)).fetchone()
    if not r:
        return DEFAULT_ASSUMPTIONS
    return dict(r._mapping)


def funnel(views: float, a: dict) -> dict:
    """Push a view count through the funnel; return each stage."""
    clicks = views * float(a["view_to_click"])
    visits = clicks * float(a["click_to_visit"])
    demos = visits * float(a["visit_to_demo"])
    deals = demos * float(a["demo_to_deal"])
    pipeline = deals * float(a["avg_deal_value"])
    return {
        "views": round(views),
        "clicks": round(clicks),
        "visits": round(visits),
        "demos": round(demos, 1),
        "deals": round(deals, 2),
        "pipeline": round(pipeline),
    }


def niche_baseline_views(session, niche: str) -> float:
    """The typical (median) video's latest views in this niche — the reference
    a client's average video is assumed to reach once established."""
    v = session.execute(text("""
        SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY p.latest_views)
        FROM video_performance p
        JOIN videos v ON v.video_id = p.video_id
        JOIN channels c ON c.channel_id = v.channel_id
        WHERE c.niche = :n AND p.latest_views IS NOT NULL
    """), {"n": niche}).scalar()
    return float(v or 0)


def recommended_multiple(session, niche: str) -> tuple[str, float]:
    """The observed outlier multiple for the strongest recommended topic — the
    empirical lift we expect from following the recommendation."""
    r = session.execute(text("""
        SELECT a.topic, ROUND(AVG(p.outlier_score), 2) AS mult, COUNT(*) n
        FROM video_attributes a
        JOIN videos v ON v.video_id = a.video_id
        JOIN channels c ON c.channel_id = v.channel_id
        JOIN video_performance p ON p.video_id = a.video_id
        WHERE c.niche = :n AND p.outlier_score IS NOT NULL AND a.topic IS NOT NULL
        GROUP BY a.topic HAVING COUNT(*) >= 3
        ORDER BY mult DESC LIMIT 1
    """), {"n": niche}).fetchone()
    if not r:
        return ("n/a", 1.0)
    return (r.topic, float(r.mult))


def project(session, niche: str = "ai-agency", videos_per_month: int = 8,
            baseline_views: float | None = None) -> dict:
    """Monthly pipeline if a client posts average content vs. content following
    the top recommendation."""
    a = latest_assumptions(session)
    if baseline_views is None:
        baseline_views = niche_baseline_views(session, niche)
    topic, mult = recommended_multiple(session, niche)

    base_per_video = funnel(baseline_views, a)
    reco_per_video = funnel(baseline_views * mult, a)

    base_month = base_per_video["pipeline"] * videos_per_month
    reco_month = reco_per_video["pipeline"] * videos_per_month
    return {
        "niche": niche,
        "assumptions": a,
        "videos_per_month": videos_per_month,
        "baseline_views_per_video": round(baseline_views),
        "recommended_topic": topic,
        "recommended_multiple": mult,
        "baseline_funnel": base_per_video,
        "recommended_funnel": reco_per_video,
        "monthly_pipeline_baseline": base_month,
        "monthly_pipeline_recommended": reco_month,
        "monthly_lift": reco_month - base_month,
    }


def _print(p: dict) -> None:
    a = p["assumptions"]
    print(f"\nROI PROJECTION — {p['niche']}\n" + "=" * 46)
    print(f"assumptions: {a['view_to_click']*100:.1f}% view->click, "
          f"{a['click_to_visit']*100:.0f}% click->visit, "
          f"{a['visit_to_demo']*100:.0f}% visit->demo, "
          f"{a['demo_to_deal']*100:.0f}% demo->deal, "
          f"${a['avg_deal_value']:.0f}/deal")
    print(f"baseline (typical video): {p['baseline_views_per_video']:,} views")
    print(f"recommended lift: '{p['recommended_topic']}' "
          f"observed {p['recommended_multiple']}x\n")
    print(f"  {'stage':<10}{'baseline':>14}{'recommended':>16}")
    for k in ["views", "clicks", "visits", "demos", "deals", "pipeline"]:
        b, r = p["baseline_funnel"][k], p["recommended_funnel"][k]
        bs = f"${b:,}" if k == "pipeline" else f"{b:,}"
        rs = f"${r:,}" if k == "pipeline" else f"{r:,}"
        print(f"  {k:<10}{bs:>14}{rs:>16}")
    print(f"\n  monthly pipeline @ {p['videos_per_month']} videos:")
    print(f"    baseline:    ${p['monthly_pipeline_baseline']:,}")
    print(f"    recommended: ${p['monthly_pipeline_recommended']:,}")
    print(f"    lift:        ${p['monthly_lift']:,}/month\n")


if __name__ == "__main__":
    seed_assumptions()
    s = SessionLocal()
    _print(project(s))
    s.close()
