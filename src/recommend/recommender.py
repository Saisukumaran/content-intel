"""The recommender — the learning loop's payoff. It reads the current pattern
analysis for a segment, combines the strongest attribute values into concrete
content ideas, scores and ranks them, and stores each idea *with the evidence
and metric state that produced it*.

Why store the evidence: it's what makes the loop demonstrable. When new
performance data shifts the underlying outlier scores, a re-run produces
different recommendations — and because every recommendation carries the
numbers it was based on, you can show "last run it recommended X because of
these scores; the data moved; now it recommends Y." That observable change,
not elapsed time, is the proof the system learns.

Scoring is deterministic: a recommendation's score is a weighted blend of the
average outlier scores of its component attributes. Topic and framing carry the
most weight because they show the largest spread in the data. Nothing here is a
generic LLM guess — every number traces back to real videos.

    python -m src.recommend.recommender            # generate + store for ai-agency
    python -m src.recommend.recommender --compare  # how the last two runs differ
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import text

from src.analysis.patterns import what_works
from src.db.session import SessionLocal

# component weights — topic & framing (angle) show the biggest spread, so they
# dominate; hook is a weak differentiator so it barely counts.
WEIGHTS = {"topic": 0.35, "angle": 0.30, "format": 0.25, "hook": 0.10}

_TOPIC_NOUN = {
    "claude": "a Claude AI Agent",
    "n8n": "an n8n Automation",
    "ai-agents": "an AI Agent",
    "automation": "an Automation System",
    "coding": "an AI Coding Workflow",
    "chatgpt": "a ChatGPT Workflow",
    "cold-email": "a Cold Email System",
    "make.com": "a Make.com Automation",
    "agency": "an AI Agency",
    "general": "an AI System",
}


def _title(topic: str, mechanic: str, fmt: str) -> str:
    noun = _TOPIC_NOUN.get(topic, "an AI System")
    if mechanic == "build-sell":
        base = f"Build & Sell {noun}"
    elif mechanic == "how-to":
        base = f"How to Build {noun}"
    elif mechanic == "listicle":
        base = f"5 Ways to Build {noun}"
    elif mechanic == "time-pressure":
        base = f"Build {noun} in a Weekend"
    elif mechanic == "dollar-figure":
        base = f"I Built {noun} and Made $10k With It"
    else:
        base = f"Build {noun}"
    if fmt == "course":
        base += " (Full Course)"
    elif fmt == "tutorial":
        base += " — Step by Step"
    return base


def _top_values(rows: list[dict], k: int, min_n: int) -> list[dict]:
    return [r for r in rows if r["n"] >= min_n][:k]


def generate(session, niche: str = "ai-agency", k: int = 5, min_n: int = 5) -> list[dict]:
    topics = _top_values(what_works(session, niche, "topic", min_n), 3, min_n)
    formats = _top_values(what_works(session, niche, "format", min_n), 2, min_n)
    mechanics = _top_values(what_works(session, niche, "title_mechanic", min_n), 2, min_n)
    hooks = _top_values(what_works(session, niche, "hook_type", min_n), 1, min_n)
    if not (topics and formats and mechanics and hooks):
        return []

    hook = hooks[0]
    candidates: list[dict] = []
    for t in topics:
        for f in formats:
            for m in mechanics:
                score = (
                    WEIGHTS["topic"] * float(t["avg_outlier"])
                    + WEIGHTS["angle"] * float(m["avg_outlier"])
                    + WEIGHTS["format"] * float(f["avg_outlier"])
                    + WEIGHTS["hook"] * float(hook["avg_outlier"])
                )
                candidates.append({
                    "idea_topic": t["value"],
                    "idea_format": f["value"],
                    "idea_hook": hook["value"],
                    "idea_angle": m["value"],          # the title mechanic / framing
                    "title": _title(t["value"], m["value"], f["value"]),
                    "score": round(score, 2),
                    "components": {
                        "topic": t, "format": f, "angle": m, "hook": hook,
                    },
                })

    # dedupe by the idea identity, keep best score, rank
    best: dict[tuple, dict] = {}
    for c in candidates:
        key = (c["idea_topic"], c["idea_format"], c["idea_angle"])
        if key not in best or c["score"] > best[key]["score"]:
            best[key] = c
    ranked_all = sorted(best.values(), key=lambda c: c["score"], reverse=True)
    selected: list[dict] = []
    angle_counts: dict[str, int] = {}
    for c in ranked_all:
        if len(selected) >= k:
            break
        angle = c["idea_angle"]
        if not selected or angle_counts.get(angle, 0) < 2:
            selected.append(c)
            angle_counts[angle] = angle_counts.get(angle, 0) + 1
    for i, c in enumerate(selected, 1):
        c["rank"] = i
    return selected


def _supporting_videos(session, niche: str, topic: str, limit: int = 3) -> list[dict]:
    rows = session.execute(text("""
        SELECT v.title, p.outlier_score
        FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        JOIN video_attributes a ON a.video_id = v.video_id
        JOIN video_performance p ON p.video_id = v.video_id
        WHERE c.niche = :niche AND a.topic = :topic
          AND p.outlier_score IS NOT NULL
        ORDER BY p.outlier_score DESC LIMIT :lim
    """), {"niche": niche, "topic": topic, "lim": limit}).fetchall()
    return [{"title": r.title, "outlier_score": float(r.outlier_score)} for r in rows]


def _rationale(c: dict) -> str:
    comp = c["components"]
    return (
        f"{comp['angle']['value']} framing averages {comp['angle']['avg_outlier']}x "
        f"and {comp['topic']['value']} topics {comp['topic']['avg_outlier']}x in this "
        f"segment; {comp['format']['value']} format adds {comp['format']['avg_outlier']}x. "
        f"Strongest combined signal available."
    )


def run(niche: str = "ai-agency") -> int:
    session = SessionLocal()
    run_id = session.execute(
        text("INSERT INTO pipeline_runs (trigger, status) "
             "VALUES ('manual-recommend','running') RETURNING id")).scalar()
    session.commit()

    recs = generate(session, niche)
    for c in recs:
        evidence = {
            "segment": niche,
            "components": {k: {"value": v["value"],
                              "avg_outlier": float(v["avg_outlier"]), "n": v["n"]}
                          for k, v in c["components"].items()},
            "supporting_videos": _supporting_videos(session, niche, c["idea_topic"]),
        }
        session.execute(text("""
            INSERT INTO recommendations
              (run_id, idea_topic, idea_format, idea_hook, idea_angle,
               rationale, evidence, score, rank, status)
            VALUES (:run, :t, :f, :h, :a, :r, :e, :s, :rank, 'auto')
        """), {"run": run_id, "t": c["idea_topic"], "f": c["idea_format"],
               "h": c["idea_hook"], "a": c["idea_angle"], "r": _rationale(c),
               "e": json.dumps(evidence), "s": c["score"], "rank": c["rank"]})

    session.execute(
        text("UPDATE pipeline_runs SET status='ok', finished_at=now(), "
             "stage_counts=:c WHERE id=:i"),
        {"c": json.dumps({"recommendations": len(recs)}), "i": run_id})
    session.commit()

    print(f"\nTOP CONTENT RECOMMENDATIONS — {niche} (run {run_id})\n" + "=" * 52)
    for c in recs:
        print(f"\n  #{c['rank']}  [score {c['score']}]  {c['title']}")
        print(f"      topic={c['idea_topic']}  format={c['idea_format']}  "
              f"framing={c['idea_angle']}")
        print(f"      why: {_rationale(c)}")
    print()
    session.close()
    return run_id


def compare_last_two(niche: str = "ai-agency") -> None:
    """Show how the top recommendation changed between the two most recent runs —
    the visible proof the loop re-derives from new data."""
    session = SessionLocal()
    runs = [r[0] for r in session.execute(text("""
        SELECT DISTINCT run_id FROM recommendations r
        JOIN pipeline_runs p ON p.id = r.run_id
        ORDER BY run_id DESC LIMIT 2
    """))]
    if len(runs) < 2:
        print("need at least two recommendation runs to compare.")
        return
    new_run, old_run = runs[0], runs[1]
    print(f"\nLOOP CHECK — run {old_run} -> run {new_run}\n" + "=" * 40)
    for label, rid in [("BEFORE", old_run), ("AFTER", new_run)]:
        top = session.execute(text("""
            SELECT rank, idea_topic, idea_angle, idea_format, score
            FROM recommendations WHERE run_id=:i ORDER BY rank LIMIT 3
        """), {"i": rid}).fetchall()
        print(f"\n{label} (run {rid}):")
        for r in top:
            print(f"  #{r.rank}  {r.idea_angle} / {r.idea_topic} / {r.idea_format}"
                  f"  (score {r.score})")
    print()
    session.close()


if __name__ == "__main__":
    if "--compare" in sys.argv:
        compare_last_two()
    else:
        run()
