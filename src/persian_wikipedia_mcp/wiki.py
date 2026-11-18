"""Persian Wikipedia tools: search, summary, article text, sections, infobox, other languages, random, trending, nearby."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field

from .http import REST, SITE, ApiError, action, fetch
from .registry import tool

DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
TAGS = re.compile(r"<[^>]+>")
CATEGORY_CAP = 40
BLANKS = re.compile(r"\n{3,}")
HEADING = re.compile(r"^(={2,6}) (.+?) \1[ \t]*$", re.MULTILINE)

Title = Annotated[str, Field(description="Article title in Persian (or as written on fa.wikipedia.org), e.g. 'تهران'.")]


def _need(title: str) -> str:
    """Return the trimmed title, or fail with a hint when it is blank."""
    if not title.strip():
        raise ApiError("title is empty. Use wiki_search to find the exact title.")
    return title.strip()


def _norm(s: str) -> str:
    """Compare key for headings: Arabic ي/ك as Persian, ZWNJ as a space, case and spacing folded."""
    return " ".join(s.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک").casefold().split())


class _Infobox(HTMLParser):
    """Collect the label/value rows of the first infobox table in rendered article HTML."""

    SKIP_TAGS = {"style", "script", "sup"}
    SKIP_CLASSES = ("geo-nondefault", "noprint", "mw-empty-elt")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0  # table nesting inside the infobox; 0 = not in it
        self.done = False
        self.skip: str | None = None
        self.skip_depth = 0
        self.cell: list | None = None  # [tag, text parts]
        self.row: list[tuple[str, str]] = []
        self.heading = ""
        self.group = ""
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if self.done:
            return
        if self.skip:
            self.skip_depth += tag == self.skip
            return
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif "infobox" in cls.split():
                self.depth = 1
            return
        if not self.depth:
            return
        if tag in self.SKIP_TAGS or any(c in cls for c in self.SKIP_CLASSES):
            self.skip, self.skip_depth = tag, 1
        elif tag == "tr" and self.depth == 1:
            self.row = []
        elif tag in ("th", "td") and self.depth == 1:
            self.cell = [tag, []]
        elif tag in ("br", "li") and self.cell:
            self.cell[1].append("; ")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip_depth -= 1
                if not self.skip_depth:
                    self.skip = None
            return
        if not self.depth or self.done:
            return
        if tag == "table":
            self.depth -= 1
            self.done = not self.depth
        elif tag in ("th", "td") and self.depth == 1 and self.cell:
            text = " ".join("".join(self.cell[1]).replace("\xa0", " ").replace("\ufeff", "").split())
            self.row.append((self.cell[0], text.replace(" ;", ";").strip(" ;")))
            self.cell = None
        elif tag == "tr" and self.depth == 1:
            if len(self.row) == 2 and self.row[0][0] == "th" and self.row[0][1] and self.row[1][1]:
                label = self.row[0][1]
                if label.startswith("•"):  # sub-item of a group header ("جمعیت" > "• شهری")
                    label = f"{self.group}: {label.lstrip('• ')}" if self.group else label.lstrip("• ")
                self.fields.setdefault(label, self.row[1][1])
            elif len(self.row) == 1 and self.row[0][0] == "th":
                if not self.heading and not self.fields:
                    self.heading = self.row[0][1]
                else:
                    self.group = self.row[0][1]

    def handle_data(self, data):
        if self.depth and not self.skip and self.cell is not None:
            self.cell[1].append(data)


def _plain(s: str) -> str:
    """Strip HTML tags and entities from an API snippet."""
    return html.unescape(TAGS.sub("", s or "")).strip()


def _url(title: str) -> str:
    return f"{SITE}/wiki/{title.replace(' ', '_')}"


def _rest_title(title: str) -> str:
    return quote(title.strip().replace(" ", "_"), safe="")


def _summary(d: dict[str, Any]) -> dict[str, Any]:
    """Trim a REST page summary to the useful fields."""
    out: dict[str, Any] = {
        "title": d.get("title"),
        "description": d.get("description"),
        "extract": d.get("extract"),
        "url": _url(d.get("title") or ""),
        "image": (d.get("thumbnail") or {}).get("source"),
        "wikidata": d.get("wikibase_item"),
        "last_edited": d.get("timestamp"),
    }
    if d.get("type") == "disambiguation":
        out["disambiguation"] = True
    if c := d.get("coordinates"):
        out["lat"], out["lon"] = c.get("lat"), c.get("lon")
    return out


async def _summary_of(title: str) -> dict[str, Any]:
    try:
        return _summary(await fetch(f"{REST}/page/summary/{_rest_title(_need(title))}"))
    except ApiError as e:
        if e.status == 404:
            raise ApiError(f"No Persian Wikipedia article titled '{title}'. Try wiki_search.", 404) from e
        raise


@tool("Search articles")
async def wiki_search(
    query: Annotated[str, Field(description="Search words, Persian or English.")],
    limit: Annotated[int, Field(ge=1, le=50, description="Results per page.")] = 10,
    offset: Annotated[int, Field(ge=0, description="Skip this many results (paging).")] = 0,
) -> dict[str, Any]:
    """Search Persian Wikipedia articles by words; returns titles with a short snippet and the total hit count."""
    r = (
        await action(
            action="query",
            list="search",
            srsearch=query,
            srlimit=limit,
            sroffset=offset,
            srprop="snippet|wordcount|timestamp",
            srinfo="totalhits|suggestion",
        )
    )["query"]
    info = r.get("searchinfo") or {}
    return {
        "total": info.get("totalhits"),
        "suggestion": info.get("suggestion"),
        "results": [
            {
                "title": h["title"],
                "snippet": _plain(h.get("snippet", "")),
                "words": h.get("wordcount"),
                "last_edited": h.get("timestamp"),
            }
            for h in r.get("search", [])
        ],
    }


@tool("Article summary")
async def wiki_summary(title: Title) -> dict[str, Any]:
    """Get the lead summary of an article: one-line description, intro paragraph, image, coordinates and URL."""
    return await _summary_of(title)


@tool("Article text")
async def wiki_article(
    title: Title,
    section: Annotated[
        str | None,
        Field(
            description="Return only this section (and its subsections), matched by heading text; see wiki_sections."
        ),
    ] = None,
    max_chars: Annotated[int, Field(ge=200, le=60000, description="Cut the text after this many characters.")] = 8000,
) -> dict[str, Any]:
    """Read an article as plain text with '== heading ==' lines, optionally one section, cut to max_chars."""
    d = await action(
        action="query", prop="extracts", explaintext=1, exsectionformat="wiki", redirects=1, titles=_need(title)
    )
    page = d["query"]["pages"][0]
    if page.get("missing") or "extract" not in page:
        raise ApiError(f"No Persian Wikipedia article titled '{title}'. Try wiki_search.", 404)
    text = BLANKS.sub("\n\n", page["extract"].strip())
    heads = [(len(m[1]), m[2].strip(), m.start(), m.end()) for m in HEADING.finditer(text)]
    out: dict[str, Any] = {"title": page["title"], "url": _url(page["title"])}
    if section:
        want = _norm(section)
        i = next((k for k, h in enumerate(heads) if _norm(h[1]) == want), None)
        if i is None:
            i = next((k for k, h in enumerate(heads) if want in _norm(h[1])), None)
        if i is None:
            raise ApiError(
                f"No section '{section}' in '{page['title']}'. Sections: " + ", ".join(h[1] for h in heads[:40]), 404
            )
        end = next((h[2] for h in heads[i + 1 :] if h[0] <= heads[i][0]), len(text))
        text = text[heads[i][2] : end].strip()
        out["section"] = heads[i][1]
    out["total_chars"] = len(text)
    out["truncated"] = len(text) > max_chars
    out["text"] = text[:max_chars]
    return out


@tool("Article sections")
async def wiki_sections(title: Title) -> dict[str, Any]:
    """List an article's section headings with their level and number (the table of contents)."""
    d = await action(action="parse", page=_need(title), prop="tocdata", redirects=1)
    p = d["parse"]
    return {
        "title": p["title"],
        "sections": [
            {"number": s["number"].translate(DIGITS), "level": s["tocLevel"], "title": s["line"]}
            for s in p["tocdata"]["sections"]
        ],
    }


@tool("Other languages")
async def wiki_languages(
    title: Title,
    lang: Annotated[
        str | None, Field(description="Only this language code, e.g. 'en' for the English article title.")
    ] = None,
) -> dict[str, Any]:
    """Get the titles of the same article in other Wikipedia languages (e.g. the English title)."""
    params: dict[str, Any] = {
        "action": "query",
        "prop": "langlinks",
        "titles": _need(title),
        "redirects": 1,
        "lllimit": "max",
        "llprop": "langname|autonym",
        "llinlanguagecode": "en",
    }
    if lang:
        params["lllang"] = lang
    page = (await action(**params))["query"]["pages"][0]
    if page.get("missing"):
        raise ApiError(f"No Persian Wikipedia article titled '{title}'. Try wiki_search.", 404)
    return {
        "title": page["title"],
        "languages": [
            {"lang": x["lang"], "name": x.get("langname"), "title": x["title"]} for x in page.get("langlinks", [])
        ],
    }


@tool("Article infobox")
async def wiki_infobox(title: Title) -> dict[str, Any]:
    """Get an article's infobox facts (birth date, population, director...) and its visible categories."""
    title = _need(title)
    d, c = await asyncio.gather(
        action(action="parse", page=title, prop="text", section=0, redirects=1, disableeditsection=1),
        action(action="query", prop="categories", clshow="!hidden", cllimit="max", redirects=1, titles=title),
    )
    p = d["parse"]
    ib = _Infobox()
    ib.feed(p["text"])
    cats = (c["query"]["pages"][0] or {}).get("categories", [])
    return {
        "title": p["title"],
        "url": _url(p["title"]),
        "infobox_title": ib.heading,
        "fields": ib.fields,
        "categories": [x["title"].partition(":")[2] for x in cats[:CATEGORY_CAP]],
        "categories_total": len(cats),
    }


@tool("Random article")
async def wiki_random() -> dict[str, Any]:
    """Get a random Persian Wikipedia article with its summary."""
    return _summary(await fetch(f"{REST}/page/random/summary", cache=False))


@tool("Trending articles")
async def wiki_trending(
    date: Annotated[
        str | None, Field(description="Day as YYYY-MM-DD (UTC). Default: the latest day with data.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50, description="Most-read articles to return.")] = 15,
) -> dict[str, Any]:
    """Get the most-read articles of a day and the picture of the day (fa has no 'on this day' or featured article)."""
    if date:
        try:
            days = [datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)]
        except ValueError as e:
            raise ApiError("date must look like 2026-11-17.") from e
    else:
        today = datetime.now(timezone.utc)
        days = [today - timedelta(days=n) for n in (1, 2, 3)]
    for day in days:
        try:
            d = await fetch(f"{REST}/feed/featured/{day:%Y/%m/%d}")
            break
        except ApiError as e:
            if e.status != 404:
                raise
            if day is days[-1]:
                raise ApiError("No most-read data for that date (try a recent day).", 404) from e
    mr = d.get("mostread") or {}
    arts = [a for a in mr.get("articles", []) if (a.get("namespace") or {}).get("id") == 0]
    out: dict[str, Any] = {
        "date": (mr.get("date") or "").rstrip("Z") or None,
        "most_read": [
            {
                "rank": i,
                "title": a["titles"]["normalized"] if a.get("titles") else a["title"].replace("_", " "),
                "views": a.get("views"),
                "description": a.get("description"),
            }
            for i, a in enumerate(arts[:limit], 1)
        ],
    }
    if img := d.get("image"):
        out["picture_of_the_day"] = {
            "title": (img.get("title") or "").removeprefix("File:"),
            "description": (img.get("description") or {}).get("text"),
            "image": (img.get("image") or {}).get("source") or (img.get("thumbnail") or {}).get("source"),
            "artist": _plain((img.get("artist") or {}).get("text") or ""),
            "license": (img.get("license") or {}).get("type"),
        }
    return out


@tool("Articles near a place")
async def wiki_nearby(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude.")],
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude.")],
    radius_m: Annotated[int, Field(ge=10, le=10000, description="Search radius in metres.")] = 1000,
    limit: Annotated[int, Field(ge=1, le=50, description="Max articles.")] = 10,
) -> dict[str, Any]:
    """Find articles about places near a latitude/longitude, nearest first, with distance in metres."""
    r = (await action(action="query", list="geosearch", gscoord=f"{lat}|{lon}", gsradius=radius_m, gslimit=limit))[
        "query"
    ]["geosearch"]
    return {
        "articles": [
            {
                "title": g["title"],
                "distance_m": round(g["dist"]),
                "lat": g["lat"],
                "lon": g["lon"],
                "url": _url(g["title"]),
            }
            for g in r
        ]
    }
