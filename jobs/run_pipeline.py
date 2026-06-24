"""The self-sustaining heartbeat. This is the single entrypoint the cron calls
on a schedule — no manual trigger. Each run: re-polls metrics (new snapshots),
tags any new videos, recomputes outlier scores, and regenerates recommendations
from the now-updated data. The recommendations sharpen on their own as the data
flows in.

    python -m jobs.run_pipeline
"""
from __future__ import annotations

from src.analysis.attributes import tag_all
from src.analysis.outlier import score_all
from src.ingestion import snapshot
from src.recommend.recommender import run as recommend


def main() -> None:
    print("[1/4] polling metrics (new snapshots)...")
    snapshot.run(trigger="cron")

    print("[2/4] tagging any new videos...")
    print(f"      tagged {tag_all()} new videos")

    print("[3/4] recomputing outlier scores...")
    print(f"      scored {score_all()} videos")

    print("[4/4] regenerating recommendations...")
    recommend()


if __name__ == "__main__":
    main()
