# Content Performance Intelligence

An agency-style content intelligence engine. It tracks a roster of YouTube
channels in one niche (AI-automation / agency-building), learns which content
attributes drive *outperformance*, recommends what to make next, proves a
content→pipeline ROI story, and re-runs itself on a schedule so the
recommendations sharpen as new data lands.

It is built as an agency would use it: we do **not** own the channels, so we
work entirely from public data and measure performance the way the market does
— **outlier score** (a video's views normalised to its own channel's baseline)
and **velocity** (how fast a fresh upload gains views over repeated captures).

## The spine

```
observe ─▶ store ─▶ learn ─▶ recommend ─▶ prove
   ▲                                          │
   └────────────── on a schedule ◀────────────┘
```

1. **Observe** — pull posts + metrics from YouTube on a schedule (`src/ingestion`)
2. **Store** — two-layer time-series DB (`migrations/`, `src/db`)
3. **Learn** — outlier scores + what's working by attribute (`src/analysis`)
4. **Recommend** — ideas + evidence that re-derive as data shifts (`src/recommend`)
5. **Prove** — transparent proxy-funnel ROI view (`src/roi`)
6. **Schedule** — GitHub Actions cron, no manual trigger (`.github/workflows`)
7. **Surface** — thin dashboard over the analysis modules (`src/dashboard`)

## Data model (the core)

Two ideas carry the design:

**Identity vs. behaviour are separate tables.** `videos` (+ `video_attributes`)
holds what a post *is* — title, format, hook, topic, angle, thumbnail features,
publish timing — and changes slowly. `metric_snapshots` holds how it's *doing*
and is **append-only**: one row per video per capture, keyed
`(video_id, captured_at)`, never updated. That split is the reason we track
*change over time* instead of a single snapshot. Velocity is just the diff
between consecutive snapshots (verified working — see `tests/`).

**The fact table is built for scale from row zero.** `metric_snapshots` is
`RANGE`-partitioned by month on `captured_at`, with a **BRIN** index on
`captured_at` (tiny, ideal for an append-only time-ordered column) and a btree
on `(video_id, captured_at DESC)` for latest-per-video and velocity lookups.
Every snapshot carries a `source` flag (`live` / `backfill` / `seeded`) so real
collected data is never confused with demo seed data.

### Scaling to 50M rows

`metric_snapshots` is the only table that explodes: rows ≈ tracked_videos ×
captures. At 50M+ rows the plan is already partly implemented, partly staged:

- **Monthly range partitions** (implemented). Queries hit one or two partitions,
  not the whole table; old months can be detached/archived in O(1).
- **BRIN over btree on time** (implemented). ~1000× smaller than a btree on an
  append-only timestamp; perfect for range scans.
- **Daily rollups** (`metric_daily`, table implemented; populated by the job).
  Analysis reads one row per video per day, not every raw snapshot — the raw
  table becomes write-mostly.
- **Retention**: keep raw snapshots hot for ~90 days, then drop the partition
  (rollups retain the history). Cold months go to cheap storage if needed.
- **Where it breaks first**: not row count — it's the YouTube API quota. A
  single key is 10,000 units/day; `videos.list` is cheap (1 unit / up to 50
  videos) but channel discovery and search are expensive. Past a few hundred
  channels you shard keys or move to a quota-aware scheduler. The DB scales long
  before the ingestion does.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in DATABASE_URL, YOUTUBE_API_KEY, OPENAI_API_KEY
alembic upgrade head          # builds the full schema incl. partitions + BRIN
python -m jobs.run_pipeline    # one manual run (the cron calls the same entrypoint)
streamlit run src/dashboard/app.py
```

`DATABASE_URL` must point at a publicly reachable Postgres (Neon / Supabase) so
the GitHub Actions cron can connect.

## Layout

```
src/
  config.py            typed settings from env (no hardcoded secrets)
  db/      models.py · session.py
  ingestion/           youtube client, back-catalog backfill, velocity snapshots
  analysis/            outlier scoring + pattern mining by attribute
  recommend/           recommender (ideas + evidence, re-derived per run)
  roi/                 proxy-funnel instrumentation
  dashboard/           streamlit (thin; reads analysis outputs)
jobs/run_pipeline.py   the scheduled entrypoint: pull → analyse → recommend
migrations/            alembic; 0001 owns the partition/BRIN/enum DDL
.github/workflows/     cron schedule
tests/
```

## Build status

- [x] Schema + migration (partitioned fact table, BRIN, rollup, attributes) — **verified against a live Postgres**
- [x] ORM models, typed config, Alembic wiring
- [ ] YouTube ingestion (backfill + scheduled velocity snapshots)
- [ ] Attribute tagging + outlier/pattern analysis
- [ ] Recommender with stored evidence (the learning loop)
- [ ] Proxy-funnel ROI view
- [ ] GitHub Actions cron
- [ ] Dashboard
