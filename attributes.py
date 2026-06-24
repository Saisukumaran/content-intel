"""Derives each video's attributes from its title, duration, and publish time
using deterministic rules. Deterministic on purpose: every tag traces to a rule
we can point at ("it's 'dollar-figure' because the title matched a currency
amount"), which is far easier to defend than a model's guess. OpenAI enrichment
can layer on top later for the fuzzier fields (nuanced hook/angle); this gives a
complete, explainable first pass with no API key.

    python -m src.analysis.attributes
"""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.models import VideoAttributes
from src.db.session import SessionLocal

# currency: $12,000  /  $5k  /  ₹85  /  "85 Rupees"
_MONEY = re.compile(r"(\$\s?\d[\d,]*k?)|(₹\s?\d[\d,]*)|(\d[\d,]*\s?(rupees|dollars))",
                    re.IGNORECASE)
_NUMBER = re.compile(r"\d")
_LISTICLE = re.compile(r"\b(\d+)\s+(tools|ways|tips|things|steps|hacks|prompts|ideas)",
                       re.IGNORECASE)
_TIME_PRESSURE = re.compile(r"\bin\s+\d+\s+(minutes?|mins?|hours?|days?|weeks?)\b",
                            re.IGNORECASE)


def title_mechanic(title: str) -> str | None:
    """One mechanic per title, highest-signal first."""
    t = title.lower()
    if _MONEY.search(title):
        return "dollar-figure"
    if "build & sell" in t or "build and sell" in t:
        return "build-sell"
    if _LISTICLE.search(title):
        return "listicle"
    if _TIME_PRESSURE.search(title):
        return "time-pressure"
    if t.startswith(("how to", "how i")):
        return "how-to"
    if re.search(r"\bi (made|built|earned|got)\b", t):
        return "result-reveal"
    if " vs " in t or " versus " in t:
        return "comparison"
    return None


def hook_type(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(stop|don't|nobody|avoid|mistake|wrong)\b", t):
        return "contrarian"
    if re.search(r"\bi (made|built|earned|got)\b", t):
        return "result-reveal"
    if t.startswith(("how to", "how i")):
        return "how-to"
    if title.strip().endswith("?") or t.startswith(("this", "the secret", "why")):
        return "curiosity"
    return "statement"


def video_format(title: str, duration: int | None) -> str:
    t = title.lower()
    if duration is not None and duration < 60:
        return "short"
    if duration is not None and duration > 3600:
        return "course"
    if _LISTICLE.search(title):
        return "listicle"
    if t.startswith(("how to", "how i")) or "tutorial" in t or "build" in t:
        return "tutorial"
    if re.search(r"\bi (made|built|earned)\b", t) or _MONEY.search(title):
        return "case-study"
    return "talk"


# coarse topic keywords for this niche — first match wins; OpenAI refines later
_TOPICS = [
    ("n8n", ["n8n"]),
    ("make.com", ["make.com", "make ", "integromat"]),
    ("cold-email", ["cold email", "cold outreach", "email outreach"]),
    ("ai-agents", ["ai agent", "agents", "agentic"]),
    ("claude", ["claude"]),
    ("chatgpt", ["chatgpt", "gpt", "openai"]),
    ("agency", ["agency", "clients", "saas", "smma"]),
    ("automation", ["automation", "automate", "workflow"]),
    ("coding", ["code", "coding", "cursor", "vibe cod"]),
]


def topic(title: str) -> str:
    t = title.lower()
    for name, kws in _TOPICS:
        if any(k in t for k in kws):
            return name
    return "general"


def duration_bucket(duration: int | None) -> str | None:
    if duration is None:
        return None
    if duration < 60:
        return "short"
    if duration < 1200:
        return "mid"
    if duration <= 3600:
        return "long"
    return "course"


def tag_all() -> int:
    """Tag every video that has no attributes row yet. Idempotent."""
    session = SessionLocal()
    rows = session.execute(text("""
        SELECT v.video_id, v.title, v.duration_seconds, v.published_at
        FROM videos v
        LEFT JOIN video_attributes a ON a.video_id = v.video_id
        WHERE a.video_id IS NULL
    """)).fetchall()

    for r in rows:
        title = r.title or ""
        pub = r.published_at
        stmt = pg_insert(VideoAttributes).values(
            video_id=r.video_id,
            hook_type=hook_type(title),
            format=video_format(title, r.duration_seconds),
            topic=topic(title),
            angle=None,                                  # left for OpenAI enrichment
            title_mechanic=title_mechanic(title),
            title_word_count=len(title.split()),
            title_has_number=bool(_NUMBER.search(title)),
            thumbnail_face=None,                         # needs vision pass (later)
            thumbnail_text_words=None,
            duration_bucket=duration_bucket(r.duration_seconds),
            publish_dow=pub.weekday() if pub else None,
            publish_hour=pub.hour if pub else None,
            method_version="rules-v1",
        ).on_conflict_do_nothing(index_elements=["video_id"])
        session.execute(stmt)

    session.commit()
    session.close()
    return len(rows)


if __name__ == "__main__":
    n = tag_all()
    print(f"tagged {n} videos (rules-v1)")
