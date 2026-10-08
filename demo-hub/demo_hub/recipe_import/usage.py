"""How many seconds of video Gemini has watched for imports today (UTC), against the hub's cap.

Google's free tier allows 8 hours of YouTube video a day; the hub stops at
``video_import_daily_seconds`` (6 h by default) so imports never use the whole day, which the
Assistant's own Gemini calls share. The count lives in ``<usage_dir>/video-<UTC date>.json``,
so it survives a restart; a day's file is never rewritten after that day.

Before a call, the video's length (from the YouTube Data API, or the estimate the shopper
confirmed on the button) is checked against what is left: over the cap is 429
daily_video_limit, with the seconds used and the limit. After a call the length is added, or,
when it is unknown, the tokens Gemini counted divided by 100 (about 100 tokens a second at low
media resolution).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo_hub.recipe_import.fetch import ImportFailure

TOKENS_PER_SECOND = 100


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class VideoUsage:
    def __init__(self, directory: str | Path, limit_s: int) -> None:
        self.dir = Path(directory).expanduser()
        self.limit_s = limit_s
        self._lock = asyncio.Lock()

    def _path(self, day: str) -> Path:
        return self.dir / f"video-{day}.json"

    def read(self, day: str | None = None) -> dict[str, Any]:
        day = day or today()
        try:
            data = json.loads(self._path(day).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"date": day, "seconds": float(data.get("seconds") or 0),
                "calls": int(data.get("calls") or 0), "limit_s": self.limit_s}

    def check(self, seconds: float | None) -> dict[str, Any]:
        """Raise 429 when ``seconds`` more (0 when unknown) would pass today's cap, or the cap
        is already reached; otherwise today's usage."""
        used = self.read()
        if used["seconds"] >= self.limit_s or used["seconds"] + (seconds or 0) > self.limit_s:
            raise ImportFailure(
                429, "daily_video_limit",
                f"Video imports have used {used['seconds'] / 3600:.1f} h of the "
                f"{self.limit_s / 3600:g} h a day the hub allows; try again tomorrow (UTC) or "
                "paste the ingredient list.", seconds_used=round(used["seconds"]),
                limit_s=self.limit_s, video_s=seconds)
        return used

    async def add(self, seconds: float | None, tokens: int) -> dict[str, Any]:
        """Count one call: its length, or tokens / 100 when the length is unknown."""
        spent = seconds if seconds else tokens / TOKENS_PER_SECOND
        async with self._lock:
            used = self.read()
            used["seconds"] = round(used["seconds"] + spent, 1)
            used["calls"] += 1
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path(used["date"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: used[k] for k in ("date", "seconds", "calls")}),
                           encoding="utf-8")
            os.replace(tmp, path)
        return used
