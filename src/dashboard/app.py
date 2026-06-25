"""Deployed dashboard. Deliberately thin: it holds almost no logic of its own,
it only *reads* from the modules that already do the work (patterns, the
recommendations table, the ROI funnel) and displays them. The engineering lives
underneath; this surfaces it. Three views — performance, recommendations, ROI —
plus a freshness strip that shows the scheduled pipeline keeping data current.

    streamlit run src/dashboard/app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# make the project root importable no matter where Streamlit launches from
# (app.py is at <root>/src/dashboard/app.py, so root is three parents up)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

st.set_page_config(page_title="Content Intelligence", layout="wide")

# Bridge: locally, config reads .env; on Streamlit Cloud, secrets arrive via
# st.secrets. Copy them into the environment before importing anything that
# builds Settings(), so the same code runs in both places.
try:
    _secrets = dict(st.secrets)
except Exception:
    _secrets = {}
for _k in ("DATABASE_URL", "YOUTUBE_API_KEY", "OPENAI_API_KEY", "TRACKED_CHANNELS"):
    if _k not in os.environ and _k in _secrets:
        os.environ[_k] = str(_secrets[_k])

import pandas as pd
from sqlalchemy import text

from src.analysis.patterns import what_works
from src.db.session import SessionLocal
from src.roi import funnel as roi

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_session():
    return SessionLocal()


# ---- freshness strip: proves the scheduled pipeline keeps data current -------
def freshness(session) -> None:
    cols = st.columns(4)
    channels = session.execute(text("SELECT count(*) FROM channels")).scalar()
    videos = session.execute(text("SELECT count(*) FROM videos")).scalar()
    snaps = session.execute(text("SELECT count(*) FROM metric_snapshots")).scalar()
    last = session.execute(text(
        "SELECT finished_at, trigger, status FROM pipeline_runs "
        "WHERE status='ok' ORDER BY id DESC LIMIT 1")).fetchone()
    cols[0].metric("Channels", channels)
    cols[1].metric("Videos", f"{videos:,}")
    cols[2].metric("Snapshots", f"{snaps:,}")
    if last and last.finished_at:
        cols[3].metric("Last pipeline run",
                       last.finished_at.strftime("%b %d %H:%M"),
                       f"{last.trigger} · {last.status}")
    else:
        cols[3].metric("Last pipeline run", "—")


# ---- performance view --------------------------------------------------------
def performance_view(session, niche: str) -> None:
    st.subheader("What's working")
    st.caption("Average outlier score (views ÷ channel median) by attribute. "
               "A group needs ≥3 videos to appear, so no single video sets a trend.")

    for attr, label in [("title_mechanic", "Title framing"),
                        ("topic", "Topic"),
                        ("format", "Format"),
                        ("publish_dow", "Publish day")]:
        rows = what_works(session, niche, attr, min_n=3)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if attr == "publish_dow":
            df["value"] = df["value"].apply(lambda v: DOW[int(v)] if v is not None else "—")
        df = df.rename(columns={"value": label, "avg_outlier": "avg outlier",
                                "n": "videos", "best": "best"})
        st.markdown(f"**{label}**")
        st.dataframe(df.set_index(label), use_container_width=True)

    st.subheader("Top videos by outlier score")
    top = session.execute(text("""
        SELECT v.title, ch.title AS channel, p.outlier_score, p.latest_views
        FROM video_performance p
        JOIN videos v ON v.video_id = p.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE ch.niche = :n AND p.outlier_score IS NOT NULL
        ORDER BY p.outlier_score DESC LIMIT 15
    """), {"n": niche}).fetchall()
    if top:
        st.dataframe(pd.DataFrame([dict(r._mapping) for r in top]),
                     use_container_width=True, hide_index=True)


# ---- recommendations view ----------------------------------------------------
def recommendations_view(session, niche: str) -> None:
    st.subheader("Recommended content to make next")
    st.caption("Derived from the patterns above, each scored by a weighted blend "
               "of the real outlier numbers behind it. Re-derived every pipeline run.")

    latest_run = session.execute(text(
        "SELECT run_id FROM recommendations ORDER BY run_id DESC LIMIT 1"
    )).scalar()
    if not latest_run:
        st.info("No recommendations yet — run `python -m src.recommend.recommender`.")
        return

    recs = session.execute(text("""
        SELECT rank, idea_topic, idea_format, idea_angle, rationale, score, evidence
        FROM recommendations WHERE run_id = :r ORDER BY rank
    """), {"r": latest_run}).fetchall()

    for r in recs:
        with st.container(border=True):
            st.markdown(f"**#{r.rank} · score {r.score}** — "
                        f"*{r.idea_angle}* framing · **{r.idea_topic}** · {r.idea_format}")
            st.write(r.rationale)
            ev = r.evidence or {}
            sv = ev.get("supporting_videos", [])
            if sv:
                st.caption("Evidence — top videos with these traits:")
                st.dataframe(pd.DataFrame(sv), use_container_width=True, hide_index=True)


# ---- ROI view ----------------------------------------------------------------
def roi_view(session, niche: str) -> None:
    st.subheader("ROI — content to projected pipeline")
    st.caption("A transparent proxy funnel. Every rate is editable — change them "
               "to a client's real numbers and the projection updates. Views are "
               "real; the lift is the observed outlier multiple for the niche.")

    baseline = roi.niche_baseline_views(session, niche)
    topic, observed_mult = roi.recommended_multiple(session, niche)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Funnel assumptions**")
        v2c = st.slider("View → click %", 0.1, 10.0, 1.5, 0.1) / 100
        c2v = st.slider("Click → visit %", 10, 100, 75, 5) / 100
        v2d = st.slider("Visit → demo %", 0.5, 15.0, 3.0, 0.5) / 100
        d2d = st.slider("Demo → deal %", 5, 60, 25, 5) / 100
        deal = st.number_input("Avg deal value ($)", 100, 100000, 1500, 100)
    with c2:
        st.markdown("**Plan**")
        vpm = st.slider("Videos per month", 1, 30, 8)
        st.caption(f"Observed lift for top topic '{topic}': **{observed_mult}x**")
        mult = st.slider("Applied lift multiple", 1.0, float(max(observed_mult, 2.0)),
                         min(3.0, float(observed_mult)), 0.5,
                         help="The observed multiple is a ceiling. A realistic "
                              "near-term target for a new client is lower.")

    a = {"view_to_click": v2c, "click_to_visit": c2v, "visit_to_demo": v2d,
         "demo_to_deal": d2d, "avg_deal_value": deal}
    base = roi.funnel(baseline, a)
    reco = roi.funnel(baseline * mult, a)

    df = pd.DataFrame({
        "stage": ["views", "clicks", "visits", "demos", "deals", "pipeline $"],
        "baseline (avg content)": [base["views"], base["clicks"], base["visits"],
                                   base["demos"], base["deals"], base["pipeline"]],
        "recommended content": [reco["views"], reco["clicks"], reco["visits"],
                                reco["demos"], reco["deals"], reco["pipeline"]],
    })
    st.dataframe(df.set_index("stage"), use_container_width=True)

    m1, m2, m3 = st.columns(3)
    m1.metric("Monthly pipeline · baseline", f"${base['pipeline']*vpm:,}")
    m2.metric("Monthly pipeline · recommended", f"${reco['pipeline']*vpm:,}")
    m3.metric("Monthly lift", f"${(reco['pipeline']-base['pipeline'])*vpm:,}")


# ---- main --------------------------------------------------------------------
def main() -> None:
    st.title("Content Performance Intelligence")
    session = get_session()
    try:
        freshness(session)
        niche = st.sidebar.selectbox("Segment", ["ai-agency", "ai-tools"])
        st.sidebar.caption("Agency lane = Saraev/Ottley. Tools lane = Vaibhav. "
                           "Segmented so patterns stay clean.")
        tab1, tab2, tab3 = st.tabs(["Performance", "Recommendations", "ROI"])
        with tab1:
            performance_view(session, niche)
        with tab2:
            recommendations_view(session, niche)
        with tab3:
            roi_view(session, niche)
    finally:
        session.close()


main()
