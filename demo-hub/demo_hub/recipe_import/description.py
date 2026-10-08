"""A YouTube description read for what a shopper can plan from: its ingredient list, and the
links that may be the creator's written recipe.

Descriptions are prose written for people: chapters ("0:00 Intro"), links to shops and social
accounts, sponsor copy, sometimes instructions aimed at a reader. Only lines from an ingredient
list leave this module (and are then read by pantry's parser, at most 60 of at most 300
characters); the rest of the description is never stored, logged or shown to a model, so
nothing a description says can reach the Assistant as an instruction.

The list is the block under an "Ingredients" heading, up to the next heading or the first
line that is not an ingredient. Without a heading, the longest run of three or more lines that
start with an amount or a bullet is taken. Both are heuristics: every line is shown to the
shopper beside its parsed amount before anything is planned.
"""

from __future__ import annotations

import re

import httpx

MAX_LINES = 60
MAX_LINE = 300
MIN_RUN = 3
MAX_LINKS = 3

INGREDIENTS_HEADING = re.compile(
    r"^\W*(ingredients?( list)?|what you('|’)?ll need|you('|’)?ll need"
    r"|shopping list)\b[^.!?]{0,40}$", re.IGNORECASE)
OTHER_HEADING = re.compile(
    r"^\W*(instructions?|method|directions?|steps?|preparation|how to make( it)?|procedure"
    r"|notes?|tips?|equipment|tools( i use)?|nutrition|timestamps?|chapters?|music|follow( me)?"
    r"|subscribe|links?|connect|social|my (gear|kitchen|cookbook|book)|recipe card"
    r"|full recipe|written recipe|printable recipe|sponsor(ed)?)\b", re.IGNORECASE)
CHAPTER = re.compile(r"^\W*\(?\d{1,2}:\d{2}(:\d{2})?\)?(\s|$)")
URL = re.compile(r"https?://[^\s<>\"'　]+", re.IGNORECASE)
BULLET = re.compile(r"^\s*([-*•▪◦·–—✓✔➤►▶"
                    r"‣⁃]|\d+[.)])\s*\S")
AMOUNT_START = re.compile(
    r"^\W{0,3}(\d|[¼½¾⅐-⅞]|(a|an|one|two|three|four|five|six|half"
    r"|pinch|dash|handful|few|some|bunch|small|medium|large)\s)", re.IGNORECASE)
SUBHEADING = re.compile(r"^\W*(for (the )?\w[\w\s]{0,30}|\w[\w\s]{0,30}):\s*$", re.IGNORECASE)

# Hosts that are never the written recipe: video and social sites, shops, link shorteners
# (where a link goes cannot be told without following it), tips and merchandise.
NOT_RECIPES = (
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.com", "fb.me",
    "tiktok.com", "twitter.com", "x.com", "threads.net", "pinterest.", "pin.it",
    "snapchat.com", "linkedin.com", "reddit.com", "discord.gg", "discord.com", "twitch.tv",
    "spotify.com", "apple.com", "amazon.", "amzn.to", "amzn.com", "a.co", "bit.ly",
    "tinyurl.com", "goo.gl", "t.co", "ow.ly", "geni.us", "linktr.ee", "lnk.to",
    "rstyle.me", "shopstyle.", "liketoknow.it", "shopltk.com", "patreon.com", "ko-fi.com",
    "buymeacoffee.com", "paypal.", "gofundme.com", "teespring.com", "spring.com",
    "etsy.com", "ebay.", "walmart.", "target.com")


def _clean(line: str) -> str:
    return " ".join(line.split())


def _ingredient_like(line: str) -> bool:
    """Starts with a bullet or an amount, and is not a heading ("Two ways to serve:")."""
    return bool(BULLET.match(line) or AMOUNT_START.match(line)) and not line.endswith(":")


def _usable(line: str) -> bool:
    """A line that can be an ingredient: not a chapter mark, not a link, not prose."""
    return bool(line) and not CHAPTER.match(line) and not URL.search(line) \
        and len(line) <= MAX_LINE


def ingredient_lines(description: str) -> list[str]:
    """The description's ingredient list, line by line and verbatim (whitespace collapsed);
    [] when it has none."""
    rows = [_clean(r) for r in (description or "").splitlines()]
    for i, row in enumerate(rows):
        if INGREDIENTS_HEADING.match(row):
            block = _block(rows[i + 1:])
            if block:
                return block[:MAX_LINES]
    return _longest_run(rows)[:MAX_LINES]


def _block(rows: list[str]) -> list[str]:
    """The lines under an ingredients heading: sub-headings ("For the sauce:") and blank lines
    are skipped, and the block ends at another heading or a line that reads as prose."""
    out: list[str] = []
    for row in rows:
        if not row:
            continue
        if OTHER_HEADING.match(row) or (INGREDIENTS_HEADING.match(row) and out):
            break
        if SUBHEADING.match(row):
            continue
        if not _usable(row):
            if out and not CHAPTER.match(row) and not URL.search(row):
                break               # a long line of prose: the list is over
            continue
        if out and not _ingredient_like(row) and len(row.split()) > 8:
            break
        out.append(row)
    return out


def _longest_run(rows: list[str]) -> list[str]:
    best: list[str] = []
    run: list[str] = []
    for row in [*rows, ""]:
        if row and _usable(row) and _ingredient_like(row):
            run.append(row)
            continue
        if len(run) > len(best):
            best = run
        run = []
    return best if len(best) >= MIN_RUN else []


def _not_a_recipe(host: str) -> bool:
    """``host`` is one of NOT_RECIPES or under one; "amazon." is Amazon on any domain."""
    for h in NOT_RECIPES:
        if h.endswith("."):
            if host.startswith(h) or f".{h}" in host:
                return True
        elif host == h or host.endswith(f".{h}"):
            return True
    return False


def recipe_links(description: str) -> list[dict[str, str]]:
    """Up to three links in the description that could be the creator's written recipe, most
    likely first: never a video, social, shop, tip or shortener link. Each is {url, site}."""
    scored: list[tuple[int, int, str, str]] = []
    seen: set[str] = set()
    for i, match in enumerate(URL.finditer(description or "")):
        url = match.group(0).rstrip(").,;:!?]}*")
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            continue
        host = parsed.host.lower()
        if not host or url in seen or _not_a_recipe(host):
            continue
        seen.add(url)
        path = parsed.path.lower()
        score = 2 * ("recipe" in path or "recipe" in host) + (path.count("-") >= 2) \
            - (path in ("", "/"))
        scored.append((-score, i, url, host.removeprefix("www.")))
    return [{"url": url, "site": site} for _, _, url, site in sorted(scored)[:MAX_LINKS]]
