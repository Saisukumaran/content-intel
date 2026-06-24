"""ORM models for application code. The authoritative schema lives in the
Alembic migration (it owns the partitioning/BRIN/enum DDL that autogenerate
can't express); these models mirror it for typed access in ingestion/analysis.
We deliberately do NOT use create_all in production — migrations own the schema.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Identity, Integer, Numeric,
    SmallInteger, Text, func,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB

# references the enum the migration already created; create_type=False so the
# ORM never tries to re-create it
snapshot_source = PGEnum(
    "live", "backfill", "seeded", name="snapshot_source", create_type=False
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"
    channel_id: Mapped[str] = mapped_column(Text, primary_key=True)
    handle: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    subscriber_count: Mapped[int | None] = mapped_column(BigInteger)
    video_count: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int | None] = mapped_column(BigInteger)
    niche: Mapped[str] = mapped_column(Text, default="ai-automation")
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stats_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    videos: Mapped[list["Video"]] = relationship(back_populates="channel")


class Video(Base):
    __tablename__ = "videos"
    video_id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.channel_id"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    category_id: Mapped[str | None] = mapped_column(Text)
    default_language: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict | None] = mapped_column(JSONB)
    captions_available: Mapped[bool | None] = mapped_column(Boolean)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    channel: Mapped["Channel"] = relationship(back_populates="videos")


class VideoAttributes(Base):
    __tablename__ = "video_attributes"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), primary_key=True)
    hook_type: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text)
    angle: Mapped[str | None] = mapped_column(Text)
    title_mechanic: Mapped[str | None] = mapped_column(Text)
    title_word_count: Mapped[int | None] = mapped_column(Integer)
    title_has_number: Mapped[bool | None] = mapped_column(Boolean)
    thumbnail_face: Mapped[bool | None] = mapped_column(Boolean)
    thumbnail_text_words: Mapped[int | None] = mapped_column(Integer)
    duration_bucket: Mapped[str | None] = mapped_column(Text)
    publish_dow: Mapped[int | None] = mapped_column(SmallInteger)
    publish_hour: Mapped[int | None] = mapped_column(SmallInteger)
    raw_llm: Mapped[dict | None] = mapped_column(JSONB)
    method_version: Mapped[str] = mapped_column(Text, default="v1")
    derived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    # composite PK mirrors the partitioned table (id, captured_at)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"))
    view_count: Mapped[int | None] = mapped_column(BigInteger)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    comment_count: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(snapshot_source, default="live")


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    idea_topic: Mapped[str | None] = mapped_column(Text)
    idea_format: Mapped[str | None] = mapped_column(Text)
    idea_hook: Mapped[str | None] = mapped_column(Text)
    idea_angle: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    score: Mapped[float | None] = mapped_column(Numeric)
    rank: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="auto")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="running")
    trigger: Mapped[str | None] = mapped_column(Text)
    stage_counts: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
