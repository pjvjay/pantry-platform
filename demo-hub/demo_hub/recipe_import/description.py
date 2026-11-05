"""A YouTube description read for what a shopper can plan from: its ingredient list, and the
links that may be the creator's written recipe.

Descriptions are prose written for people: chapters ("0:00 Intro"), links to shops and social
accounts, sponsor copy, sometimes instructions aimed at a reader. Only lines from an ingredient
list leave this module (and are then read by pantry's parser, at most 60 of at most 300
characters); the rest of the description is never stored, logged or shown to a model, so
nothing a description says can reach the Assistant as an instruction.

The list is the block under an "Ingredients" heading, up to the next heading, a method step
("2. Boil the water") or the first line that is not an ingredient; a block of one or two lines
counts only when each starts with a bullet or an amount. Without a heading, the longest run of
three or more lines that start with an amount (after any bullet or list number) is taken, so a
method written as numbered steps is never read as ingredients. Both are heuristics: in the
import sheet every line is shown beside its parsed amount before anything is planned, but a
link pasted in chat is planned straight away, so the heuristics lean towards finding no list
(the shopper then chooses how to read it) over reading prose as one.
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
# a bullet or a list number ("1.", "2)") in front of a line's own words
MARKER = re.compile(r"^\s*(?:[-*•▪◦·–—✓✔➤►▶‣⁃]\s*|\d+[.)]\s+)+")
# the first word of a method step ("1. Boil the water", "- Stir in the cream"). Such a line is
# never an ingredient, and under an ingredients heading it means the steps have begun.
STEP = re.compile(
    r"^(add|bake|beat|blend|boil|bring|chop|combine|cook|cover|cut|drain|fold|fry|heat|knead"
    r"|let|marinate|melt|mix|place|pour|preheat|put|reduce|remove|rinse|roast|saut[eé]|serve"
    r"|simmer|soak|spread|sprinkle|stir|strain|toss|transfer|whisk)\b", re.IGNORECASE)

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


def _words(line: str) -> str:
    """The line without its bullet or list number: "2. Boil the water" -> "Boil the water"."""
    return MARKER.sub("", line, count=1)


def _amount_first(line: str) -> bool:
    """Starts with an amount once any bullet or list number is set aside ("1. 2 cups rice",
    "- 1 egg", "200 g lentils"), unlike a numbered step ("1. Boil the water")."""
    return bool(AMOUNT_START.match(_words(line))) and not line.endswith(":")


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
            # a heading says a list follows, so two lines can be one ("2 cups rice", "1
            # onion"); a short block that does not read as a list ("Thanks for watching!") is not
            if len(block) >= MIN_RUN or (block and all(map(_ingredient_like, block))):
                return block[:MAX_LINES]
    return _longest_run(rows)[:MAX_LINES]


def _block(rows: list[str]) -> list[str]:
    """The lines under an ingredients heading: sub-headings ("For the sauce:") and blank lines
    are skipped, and the block ends at another heading, a method step or a line that reads as
    prose."""
    out: list[str] = []
    for row in rows:
        if not row:
            continue
        if OTHER_HEADING.match(row) or (INGREDIENTS_HEADING.match(row) and out) \
                or STEP.match(_words(row)):
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
    """With no heading to say a list follows, only lines that start with an amount: a bullet
    or a number alone also starts every line of a method written as steps."""
    best: list[str] = []
    run: list[str] = []
    for row in [*rows, ""]:
        if row and _usable(row) and _amount_first(row):
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
