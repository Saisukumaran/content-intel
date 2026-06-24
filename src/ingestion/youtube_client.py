"""Talks to the YouTube Data API v3. This layer knows nothing about our
database — it just fetches and returns clean Python dicts. Keeping the API
layer and the storage layer separate makes both easy to test and explain.

Why raw httpx instead of the google-api-python-client SDK: the SDK hides the
exact thing this challenge asks us to demonstrate — how we handle pagination,
rate limits, and retries. Writing it ourselves keeps that logic visible.

Quota: YouTube gives 10,000 units/day on a free key. The calls we use are
cheap — channels.list, playlistItems.list and videos.list are 1 unit each
(videos.list returns up to 50 videos per unit). We count units as we go and
stop before blowing the daily budget rather than getting surprised by a 403.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

import httpx
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt, wait_exponential,
)

API_BASE = "https://www.googleapis.com/youtube/v3"


class QuotaExceeded(RuntimeError):
    """Daily quota is gone — retrying won't help, so we stop cleanly."""


class TransientYouTubeError(RuntimeError):
    """A 5xx or rate-limit blip — safe to retry with backoff."""


# PT1H2M10S -> 3730 seconds. We parse this ourselves rather than add a library.
_DURATION_RE = re.compile(
    r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
)


def parse_duration(iso: str | None) -> int | None:
    if not iso:
        return None
    m = _DURATION_RE.fullmatch(iso)
    if not m:
        return None
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + s


@dataclass
class YouTubeClient:
    api_key: str
    quota_budget: int = 9000          # leave headroom under the 10k ceiling
    quota_used: int = field(default=0, init=False)
    _client: httpx.Client = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(base_url=API_BASE, timeout=30.0)

    def close(self) -> None:
        if self._client:
            self._client.close()

    def __enter__(self) -> "YouTubeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low-level GET with retry/backoff and quota accounting -------------
    @retry(
        retry=retry_if_exception_type(TransientYouTubeError),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, endpoint: str, params: dict, cost: int = 1) -> dict:
        if self.quota_used + cost > self.quota_budget:
            raise QuotaExceeded(
                f"Would exceed quota budget ({self.quota_used}/{self.quota_budget})"
            )
        params = {**params, "key": self.api_key}
        resp = self._client.get(f"/{endpoint}", params=params)

        if resp.status_code == 200:
            self.quota_used += cost
            return resp.json()

        # classify the error so we retry only what's worth retrying
        reason = ""
        try:
            reason = resp.json()["error"]["errors"][0].get("reason", "")
        except Exception:
            pass
        if resp.status_code == 403 and reason in {"quotaExceeded", "dailyLimitExceeded"}:
            raise QuotaExceeded(reason)
        if resp.status_code == 429 or resp.status_code >= 500 or reason == "rateLimitExceeded":
            raise TransientYouTubeError(f"{resp.status_code} {reason}")
        # 400s (bad handle, etc.) are real bugs — surface them loudly
        resp.raise_for_status()
        return resp.json()

    # -- channel: resolve @handle -> id, stats, uploads playlist ----------
    def get_channel(self, handle: str) -> dict | None:
        handle = handle.lstrip("@")
        data = self._get(
            "channels",
            {"part": "snippet,statistics,contentDetails", "forHandle": handle},
            cost=1,
        )
        items = data.get("items") or []
        if not items:
            return None
        it = items[0]
        stats = it.get("statistics", {})
        return {
            "channel_id": it["id"],
            "handle": f"@{handle}",
            "title": it["snippet"]["title"],
            "subscriber_count": int(stats.get("subscriberCount", 0)) or None,
            "video_count": int(stats.get("videoCount", 0)) or None,
            "view_count": int(stats.get("viewCount", 0)) or None,
            "uploads_playlist": it["contentDetails"]["relatedPlaylists"]["uploads"],
        }

    # -- all video ids in a channel, paginated ----------------------------
    def iter_video_ids(self, uploads_playlist: str) -> Iterator[str]:
        page_token = None
        while True:
            params = {
                "part": "contentDetails",
                "playlistId": uploads_playlist,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get("playlistItems", params, cost=1)
            for item in data.get("items", []):
                vid = item["contentDetails"].get("videoId")
                if vid:
                    yield vid
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    # -- full metadata + current metrics, batched 50 at a time ------------
    def get_videos(self, video_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            data = self._get(
                "videos",
                {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)},
                cost=1,
            )
            for it in data.get("items", []):
                snip = it.get("snippet", {})
                stats = it.get("statistics", {})
                content = it.get("contentDetails", {})
                thumbs = snip.get("thumbnails", {})
                best = thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default", {})
                out.append({
                    "video_id": it["id"],
                    "channel_id": snip.get("channelId"),
                    "title": snip.get("title", ""),
                    "description": snip.get("description"),
                    "published_at": snip.get("publishedAt"),
                    "duration_seconds": parse_duration(content.get("duration")),
                    "category_id": snip.get("categoryId"),
                    "default_language": snip.get("defaultAudioLanguage") or snip.get("defaultLanguage"),
                    "thumbnail_url": best.get("url"),
                    "tags": snip.get("tags"),
                    "captions_available": content.get("caption") == "true",
                    # current metrics — become the snapshot rows
                    "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
                    "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
                    "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
                })
        return out
