"""What a model call would cost on a paid key: tokens times the provider's list price.

Local models (``ollama:``) cost nothing per call (the machine's power is not counted here). Gemini
prices are Google's paid-tier list prices per million tokens, input (prompt) and output (the
reply, thinking included); the free tier bills nothing but caps requests per model per day. A
model missing from the table reports ``None`` rather than a guess.
"""

from __future__ import annotations

# USD per 1M tokens: (input, output), Standard paid tier, prompts up to 200k tokens.
PRICES: dict[str, tuple[float, float]] = {
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (0.75, 3.75),        # introductory through 2026-12-31, then 1.50 / 7.50
    "gemini-3.7-flash": (0.75, 3.75),        # introductory through 2026-12-31, then 1.50 / 7.50
    "gemini-3.8-flash": (0.75, 3.75),        # introductory through 2026-12-31, then 1.50 / 7.50
    "gemini-3.1-pro-preview": (2.00, 12.00),  # 4.00 / 18.00 above 200k prompt tokens; no free tier
}
# The -latest aliases are left out: Google does not publish what they point to today.
PRICES_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing (last updated 2026-10-01 UTC)"
# What Google's own 429 reports for the free tier: requests per model per day, reset at midnight
# Pacific (GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit 20, seen 2026-10-04).
FREE_TIER_REQUESTS_PER_DAY = 20


def _name(model: str) -> tuple[str, str]:
    provider, _, rest = model.partition(":")
    return provider, rest.split("#", 1)[0]


def price_of(model: str) -> tuple[float, float] | None:
    provider, name = _name(model)
    if provider == "ollama":
        return (0.0, 0.0)
    return PRICES.get(name)


def call_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = price_of(model)
    if price is None:
        return None
    return round(input_tokens * price[0] / 1e6 + output_tokens * price[1] / 1e6, 6)


# A video import (recipe_import/gemini_video.py) is priced apart from chat: YouTube URL input is
# a preview "at no charge", so its rate comes from the hub's settings (DEMO_VIDEO_IMPORT_PRICE,
# 0 by default) and every figure carries this label rather than passing for a list price.
VIDEO_IMPORT_PRICING = "preview pricing"


def video_import(model: str, usage: dict, price: tuple[float, float]) -> dict:
    """Gemini's usageMetadata for one video import as the hub records it: prompt, output and
    total tokens, the per-modality prompt counts when Gemini gives them (VIDEO, AUDIO, TEXT),
    and the cost at ``price`` (USD per 1M tokens, input and output). Thinking tokens are
    billed as output."""
    prompt = int(usage.get("promptTokenCount") or 0)
    output = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount")
                                                               or 0)
    by_modality = {str(d.get("modality", "")).lower(): int(d.get("tokenCount") or 0)
                   for d in usage.get("promptTokensDetails") or [] if isinstance(d, dict)}
    return {"kind": "video_import", "model": model, "prompt_tokens": prompt,
            "output_tokens": output,
            "total_tokens": int(usage.get("totalTokenCount") or prompt + output),
            "by_modality": by_modality,
            "llm_cost_usd": round(prompt * price[0] / 1e6 + output * price[1] / 1e6, 6),
            "pricing": VIDEO_IMPORT_PRICING}
