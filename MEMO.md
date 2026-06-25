# Content Performance Intelligence, Technical Memo

**What it is:** an agency-style system that tracks a roster of YouTube channels
in one niche (AI-automation / agency-building), learns which content attributes
drive outperformance, recommends what to make next, proves a content-to-pipeline
ROI story, and re-runs itself on a schedule so the recommendations sharpen as new
data arrives. We don't own the channels, we analyse public data the way an
agency entering a niche would.

Live dashboard: `sai-content-intel.streamlit.app`
Repo: `github.com/Saisukumaran/content-intel`

---

## 1. Architecture and data model

The system is a five-stage pipeline, **observe → store → learn → recommend →
prove**, wrapped in a scheduler so it runs without a manual trigger. Each stage
is one module (`ingestion`, `analysis`, `recommend`, `roi`, `dashboard`), kept
separate so each is testable and defensible on its own.

**The data model rests on two decisions.**

*Identity is separated from behaviour.* `videos` (plus `video_attributes`) holds
what a post *is*, title, format, hook, topic, angle, thumbnail and timing, 
and changes slowly. `metric_snapshots` holds how it's *doing* and is
append-only: one row per video per capture, keyed `(video_id, captured_at)`,
never updated. That split is the reason the system tracks change over time
rather than a single snapshot. Velocity is simply the difference between
consecutive snapshot rows.

*The fact table is built for scale from row zero.* `metric_snapshots` is the
only table that grows without bound (videos × captures), so it is range-
partitioned by month on `captured_at`, with a **BRIN** index on `captured_at`
(tiny and ideal for an append-only, time-ordered column) and a btree on
`(video_id, captured_at DESC)` for latest-per-video and velocity lookups. Every
snapshot carries a `source` flag (`live` / `backfill` / `seeded`) so real
collected data is never confused with demo seed data. Schema changes are managed
with real Alembic migrations; the partition/BRIN/enum DDL is hand-written
because autogenerate can't express it.

**Scaling to 50M rows.** Monthly partitions keep queries scoped to one or two
partitions and let old months be archived in O(1). A `metric_daily` rollup lets
analysis read one row per video per day instead of every raw snapshot, so the
raw table becomes write-mostly. Retention drops raw partitions past ~90 days
while rollups keep the history. The honest punchline: **the database scales long
before the ingestion does: the real ceiling is the YouTube API quota**, not row
count.

Current state: 3 channels, ~1,191 videos, snapshots accumulating every 2 hours,
~55 quota units used against a 10,000/day budget.

## 2. Platforms, and why

**YouTube only, done properly.** It has a free API with rich public metrics
(views, likes, comments), clean pagination, and public thumbnails and captions, 
enough to analyse packaging, structure, and engagement without owning the
channel. X is paywalled and locked down; RSS gives posts but almost no
engagement signal. One platform analysed well beats three analysed shallowly,
and the ingestion layer is written so a second source is a new module, not a
rewrite.

We don't own the channels, so we can't see owner-only analytics (CTR,
retention). Instead we measure performance two public ways: **outlier score**
(views ÷ the channel's own median, so a 40k-sub and a 400k-sub channel are
comparable, the same approach Rare Social productised) and **velocity** (how
fast a fresh upload gains views across repeated captures). Velocity is arguably
cleaner than an owner's retention graph: it's the market's actual verdict,
measured over time.

## 3. Analysis, recommendations, and the learning loop

**Analysis.** Each video is tagged with deterministic rules (`rules-v1`):
title mechanic (dollar-figure, build-sell, listicle…), format, topic, hook,
duration bucket, publish timing. Deterministic on purpose, every tag traces to
a rule we can point at, which is easier to defend than a model's guess. We then
compute the outlier score per video and group by attribute to surface what's
working, **segmented by sub-niche** (agency vs. AI-tools) so patterns stay
clean. Real output: build-sell framing averages ~33x, ai-agents/claude topics
~8–9x, long-form course format ~7.9x; Shorts underperform at 0.66x.

**Recommendations.** The recommender combines the strongest topic, framing, and
format into concrete ideas, scored by a weighted blend of the real outlier
numbers behind each. Every recommendation is stored *with the evidence and
metric state that produced it*. A diversity rule caps how often one framing
repeats, so the top list is a spread of distinct ideas, not five flavours of the
same one, at a transparent, visible cost in score.

**Why the loop sharpens.** Because recommendations are derived from the
accumulated snapshot data and re-generated on every scheduled run, they change
when the data changes, automatically, with no human curation. The stored
evidence makes this demonstrable: inject a new snapshot where a topic surges,
re-run, and the recommendation re-ranks with new evidence behind it. That
observable change, not elapsed time, is the proof the system learns. A human
promote/veto layer can sit on top as a quality gate, but the base learning is
automatic.

## 4. ROI, and why a client would trust it

We have no real conversion data, so we instrument a transparent proxy funnel:
**views → clicks → site visits → demo requests → deals → pipeline $**. Two
things make it credible. First, every conversion rate is explicit, versioned,
and editable in the dashboard, a client plugs in their own funnel and the model
updates live; nothing is hidden in code. Second, the lift is grounded in real
observed data: baseline is the niche's median video, and "recommended" applies
the actual outlier multiple measured for the recommended attributes. So the ROI
view is the whole system monetised, we learned what outperforms, we recommend
it, and we price the difference between following that advice and posting average
content.

Honest framing: the observed multiple (e.g. ~8.8x) is a ceiling drawn from
established creators, not a forecast for a new client. The model takes the
multiple as an input, so it's presented as a range, 1x baseline, a conservative
near-term target, and the observed ceiling, rather than a single promised
number. The transparency about it being a projection is what earns trust.

## 5. Cost at scale, and where it breaks first

Costs are near-zero at this scale and stay low: managed Postgres (Neon free
tier), GitHub Actions for the cron (free minutes), Streamlit Community Cloud for
the dashboard (free). The only metered external dependency is the YouTube API.

**Where it breaks first: the API quota, not the database.** At 10,000 units/day,
the cheap calls (`videos.list` at 1 unit per 50 videos) scale to a few hundred
channels comfortably, but channel discovery and search are expensive, and
frequent polling of many channels adds up. Past a few hundred channels you'd
shard API keys or move to a quota-aware scheduler. The database would handle far
more rows before becoming the constraint, which is why ingestion is written by
hand (full control over pagination, retries, dedup, and quota accounting) rather
than paying an ETL tool per row.

**Build vs. buy.** Built: ingestion, schema, scoring, the recommender, the core
IP and the things that cost-per-row to rent. Bought: managed Postgres, hosting,
the scheduler, the charting layer. Tools like Rare Social exist for outlier
scoring; for a single niche, computing it ourselves from raw view counts is
cheaper and gives full control.

---

## Honest limitations

- **Short live-data window.** Real velocity history is days, not months, because
  YouTube only exposes current totals, historical daily views for others'
  videos can't be reconstructed. The back-catalog gives breadth; velocity gives
  a real but short time series; the loop is proven by mechanism, not duration.
- **ROI is a projection**, not measured conversions (see §4).
- **Single platform and rules-based tagging**, both designed as the first,
  defensible version, with clear extension points (a second ingestion module;
  LLM enrichment layered over the rules for fuzzier hook/angle nuance).

Everything in this memo is running on real data, tested, and deployed.
