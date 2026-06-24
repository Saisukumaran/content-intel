"""initial schema: channels, videos, attributes, append-only metric snapshots,
recommendations, ROI assumptions, pipeline runs.

The design has two load-bearing ideas:

  1. Identity vs. behaviour are separated. `videos` (+ `video_attributes`) holds
     what a post *is* and changes slowly. `metric_snapshots` holds how it is
     *doing*, and is append-only — one row per video per capture. That split is
     the reason we can track change over time instead of a single snapshot.

  2. `metric_snapshots` is range-partitioned by month with a BRIN index on
     captured_at. This is the table that explodes (videos x captures), so it is
     built for scale from row zero. See the README "Scaling to 50M rows" note.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- enums -------------------------------------------------------------
    op.execute(
        "CREATE TYPE snapshot_source AS ENUM ('live', 'backfill', 'seeded')"
    )

    # --- channels ----------------------------------------------------------
    # One row per tracked channel. Stats here are the *current* values; the
    # performance baseline used for outlier scoring is recomputed in analysis.
    op.execute(
        """
        CREATE TABLE channels (
            channel_id        TEXT PRIMARY KEY,          -- YouTube channel id (UC...)
            handle            TEXT,
            title             TEXT,
            subscriber_count  BIGINT,
            video_count       INTEGER,
            view_count        BIGINT,
            niche             TEXT NOT NULL DEFAULT 'ai-automation',
            tracked           BOOLEAN NOT NULL DEFAULT TRUE,
            first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            stats_fetched_at  TIMESTAMPTZ
        )
        """
    )

    # --- videos (slowly-changing identity) ---------------------------------
    op.execute(
        """
        CREATE TABLE videos (
            video_id          TEXT PRIMARY KEY,          -- YouTube video id
            channel_id        TEXT NOT NULL REFERENCES channels(channel_id),
            title             TEXT NOT NULL,
            description       TEXT,
            published_at      TIMESTAMPTZ NOT NULL,
            duration_seconds  INTEGER,
            category_id       TEXT,
            default_language  TEXT,
            thumbnail_url     TEXT,
            tags              JSONB,
            captions_available BOOLEAN,
            first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_videos_channel ON videos(channel_id)")
    op.execute("CREATE INDEX idx_videos_published ON videos(published_at DESC)")

    # --- video_attributes (the learned vocabulary, 1:1 with videos) --------
    # method_version lets us re-derive attributes later without losing history
    # of how a given video was tagged.
    op.execute(
        """
        CREATE TABLE video_attributes (
            video_id            TEXT PRIMARY KEY REFERENCES videos(video_id),
            hook_type           TEXT,        -- e.g. 'contrarian', 'how-to', 'result-reveal'
            format              TEXT,        -- e.g. 'tutorial', 'listicle', 'case-study'
            topic               TEXT,        -- e.g. 'n8n', 'cold-email', 'agency-scaling'
            angle               TEXT,        -- e.g. 'make-money', 'tool-teardown'
            title_mechanic      TEXT,        -- 'dollar-figure','time-pressure','expert-reacts'...
            title_word_count    INTEGER,
            title_has_number    BOOLEAN,
            thumbnail_face      BOOLEAN,
            thumbnail_text_words INTEGER,
            duration_bucket     TEXT,        -- 'short','mid','long','course'
            publish_dow         SMALLINT,    -- 0=Mon .. 6=Sun
            publish_hour        SMALLINT,    -- 0..23 (channel-local handled in code)
            raw_llm             JSONB,       -- full model output for auditability
            method_version      TEXT NOT NULL DEFAULT 'v1',
            derived_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_attr_topic ON video_attributes(topic)")
    op.execute("CREATE INDEX idx_attr_format ON video_attributes(format)")
    op.execute("CREATE INDEX idx_attr_hook ON video_attributes(hook_type)")

    # --- metric_snapshots (append-only fact table, the heartbeat) ----------
    # Range-partitioned by month on captured_at. The UNIQUE(video_id,
    # captured_at) constraint includes the partition key (captured_at) as PG
    # requires. source flags whether a row was really collected ('live'/
    # 'backfill') or inserted for a demo ('seeded') — nothing is ever ambiguous.
    op.execute(
        """
        CREATE TABLE metric_snapshots (
            id            BIGINT GENERATED ALWAYS AS IDENTITY,
            video_id      TEXT NOT NULL REFERENCES videos(video_id),
            captured_at   TIMESTAMPTZ NOT NULL,
            view_count    BIGINT,
            like_count    BIGINT,
            comment_count BIGINT,
            source        snapshot_source NOT NULL DEFAULT 'live',
            PRIMARY KEY (id, captured_at),
            UNIQUE (video_id, captured_at)
        ) PARTITION BY RANGE (captured_at)
        """
    )
    # btree for point lookups / latest-per-video / velocity diffs
    op.execute(
        "CREATE INDEX idx_snap_video_time "
        "ON metric_snapshots (video_id, captured_at DESC)"
    )
    # BRIN is tiny and ideal for an append-only, time-ordered column at scale
    op.execute(
        "CREATE INDEX idx_snap_captured_brin "
        "ON metric_snapshots USING BRIN (captured_at)"
    )
    # Initial partitions: previous, current, next month, plus a catch-all
    # default so an insert never fails if a partition is missing. The scheduled
    # job calls ensure_partition() to roll future months automatically.
    op.execute(
        """
        DO $$
        DECLARE
            m DATE := date_trunc('month', now())::date - INTERVAL '1 month';
            i INTEGER;
            start_d DATE;
            end_d DATE;
            pname TEXT;
        BEGIN
            FOR i IN 0..2 LOOP
                start_d := m + (i || ' month')::interval;
                end_d   := start_d + INTERVAL '1 month';
                pname   := 'metric_snapshots_' || to_char(start_d, 'YYYY_MM');
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF metric_snapshots '
                    'FOR VALUES FROM (%L) TO (%L)', pname, start_d, end_d);
            END LOOP;
        END $$;
        """
    )
    op.execute(
        "CREATE TABLE metric_snapshots_default "
        "PARTITION OF metric_snapshots DEFAULT"
    )

    # helper the cron uses to guarantee next month's partition exists
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_partition(target DATE)
        RETURNS void AS $$
        DECLARE
            start_d DATE := date_trunc('month', target)::date;
            end_d   DATE := start_d + INTERVAL '1 month';
            pname   TEXT := 'metric_snapshots_' || to_char(start_d, 'YYYY_MM');
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = pname) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF metric_snapshots '
                    'FOR VALUES FROM (%L) TO (%L)', pname, start_d, end_d);
            END IF;
        END $$ LANGUAGE plpgsql;
        """
    )

    # --- metric_daily (rollup for scale) -----------------------------------
    # Analysis reads this, not raw snapshots, once data is large. Populated by
    # the pipeline. See README scaling note.
    op.execute(
        """
        CREATE TABLE metric_daily (
            video_id        TEXT NOT NULL REFERENCES videos(video_id),
            day             DATE NOT NULL,
            last_view_count BIGINT,
            view_delta      BIGINT,
            like_delta      BIGINT,
            comment_delta   BIGINT,
            PRIMARY KEY (video_id, day)
        )
        """
    )

    # --- video_performance (materialized analysis output, refreshed per run)-
    op.execute(
        """
        CREATE TABLE video_performance (
            video_id         TEXT PRIMARY KEY REFERENCES videos(video_id),
            as_of            TIMESTAMPTZ NOT NULL,
            latest_views     BIGINT,
            channel_baseline NUMERIC,     -- channel median views at as_of
            outlier_score    NUMERIC,     -- latest_views / channel_baseline
            views_per_day    NUMERIC,     -- lifetime avg
            velocity_recent  NUMERIC,     -- views/hour from recent snapshots
            accelerating     BOOLEAN
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_perf_outlier ON video_performance(outlier_score DESC)"
    )

    # --- pipeline_runs (observability; proves the cron self-sustains) ------
    op.execute(
        """
        CREATE TABLE pipeline_runs (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at  TIMESTAMPTZ,
            status       TEXT NOT NULL DEFAULT 'running',  -- running|ok|error
            trigger      TEXT,                              -- 'cron'|'manual'
            stage_counts JSONB,
            error        TEXT
        )
        """
    )

    # --- roi_assumptions (versioned, transparent funnel rates) -------------
    op.execute(
        """
        CREATE TABLE roi_assumptions (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            version          TEXT NOT NULL,
            view_to_click    NUMERIC NOT NULL,   -- proxy CTR off-platform
            click_to_visit   NUMERIC NOT NULL,
            visit_to_demo    NUMERIC NOT NULL,
            demo_to_deal     NUMERIC NOT NULL,
            avg_deal_value   NUMERIC NOT NULL,
            note             TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- recommendations (append-only; each stored WITH its evidence) ------
    # Storing the evidence + metric state at generation time is what makes the
    # learning loop *demonstrable*: you can show rec was X, data moved, rec is
    # now Y, and point at the evidence behind each.
    op.execute(
        """
        CREATE TABLE recommendations (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id       BIGINT REFERENCES pipeline_runs(id),
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            idea_topic   TEXT,
            idea_format  TEXT,
            idea_hook    TEXT,
            idea_angle   TEXT,
            rationale    TEXT,
            evidence     JSONB,        -- supporting video_ids + scores at gen time
            score        NUMERIC,      -- ranking metric
            rank         INTEGER,
            status       TEXT NOT NULL DEFAULT 'auto'  -- auto|promoted|vetoed
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_recs_generated ON recommendations(generated_at DESC)"
    )


def downgrade() -> None:
    for tbl in [
        "recommendations", "roi_assumptions", "pipeline_runs",
        "video_performance", "metric_daily",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS ensure_partition(DATE)")
    op.execute("DROP TABLE IF EXISTS metric_snapshots CASCADE")
    op.execute("DROP TABLE IF EXISTS video_attributes CASCADE")
    op.execute("DROP TABLE IF EXISTS videos CASCADE")
    op.execute("DROP TABLE IF EXISTS channels CASCADE")
    op.execute("DROP TYPE IF EXISTS snapshot_source")
