from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.anyio

TOOLS = {
    "wiki_search",
    "wiki_summary",
    "wiki_article",
    "wiki_sections",
    "wiki_infobox",
    "wiki_languages",
    "wiki_random",
    "wiki_trending",
    "wiki_nearby",
}
EXTRACT = "Lead text.\n\n\n== One ==\nBody one.\n=== Sub ===\nBody sub.\n== Two ==\nBody two."
SUMMARY = {
    "type": "standard",
    "title": "تهران",
    "description": "پایتخت",
    "extract": "تهران ...",
    "thumbnail": {"source": "https://upload.wikimedia.org/x.jpg"},
    "wikibase_item": "Q3616",
    "timestamp": "2026-11-15T10:22:14Z",
    "coordinates": {"lat": 35.7, "lon": 51.4},
}


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name


async def test_search_strips_html(client, api):
    api["/w/api.php"] = {
        "query": {
            "searchinfo": {"totalhits": 2, "suggestion": "تهران"},
            "search": [{"title": "تهران", "snippet": 'a <span class="searchmatch">b</span> &amp; c', "wordcount": 9}],
        }
    }
    r = (await client.call_tool("wiki_search", {"query": "تهرن"})).structured_content
    assert r["total"] == 2 and r["suggestion"] == "تهران"
    assert r["results"][0]["snippet"] == "a b & c"
    assert api.calls[0].url.params["srsearch"] == "تهرن"
    assert "persian-wikipedia-mcp" in api.calls[0].headers["user-agent"]


async def test_summary_and_not_found(client, api):
    api["/api/rest_v1/page/summary/تهران"] = SUMMARY
    r = (await client.call_tool("wiki_summary", {"title": "تهران"})).structured_content
    assert r["title"] == "تهران" and r["lat"] == 35.7 and r["wikidata"] == "Q3616"
    assert r["url"] == "https://fa.wikipedia.org/wiki/تهران"
    bad = await client.call_tool("wiki_summary", {"title": "نیست"})
    assert bad.is_error and "wiki_search" in bad.content[0].text


async def test_article_sections_and_limit(client, api):
    api["/w/api.php"] = {"query": {"pages": [{"title": "X", "extract": EXTRACT}]}}
    full = (await client.call_tool("wiki_article", {"title": "X"})).structured_content
    assert full["total_chars"] == len(EXTRACT) - 1 and not full["truncated"]
    cut = (await client.call_tool("wiki_article", {"title": "X", "max_chars": 200})).structured_content
    assert cut["text"] == full["text"]
    one = (await client.call_tool("wiki_article", {"title": "X", "section": "one"})).structured_content
    assert "Body sub." in one["text"] and "Body two." not in one["text"] and one["section"] == "One"
    sub = (await client.call_tool("wiki_article", {"title": "X", "section": "Sub"})).structured_content
    assert sub["text"].endswith("Body sub.")
    miss = await client.call_tool("wiki_article", {"title": "X", "section": "zzz"})
    assert miss.is_error and "One" in miss.content[0].text


async def test_article_missing(client, api):
    api["/w/api.php"] = {"query": {"pages": [{"title": "Y", "missing": True}]}}
    assert (await client.call_tool("wiki_article", {"title": "Y"})).is_error


async def test_sections_ascii_numbers(client, api):
    api["/w/api.php"] = {
        "parse": {"title": "X", "tocdata": {"sections": [{"tocLevel": 2, "number": "۱.۲", "line": "زیر"}]}}
    }
    r = (await client.call_tool("wiki_sections", {"title": "X"})).structured_content
    assert r["sections"] == [{"number": "1.2", "level": 2, "title": "زیر"}]


async def test_languages(client, api):
    api["/w/api.php"] = {
        "query": {
            "pages": [{"title": "تهران", "langlinks": [{"lang": "en", "langname": "English", "title": "Tehran"}]}]
        }
    }
    r = (await client.call_tool("wiki_languages", {"title": "تهران", "lang": "en"})).structured_content
    assert r["languages"] == [{"lang": "en", "name": "English", "title": "Tehran"}]
    assert api.calls[0].url.params["lllang"] == "en"


async def test_random_follows_redirect(client, api):
    api["/api/rest_v1/page/random/summary"] = lambda r: httpx.Response(
        303, headers={"location": "https://fa.wikipedia.org/api/rest_v1/page/summary/abc"}
    )
    api["/api/rest_v1/page/summary/abc"] = SUMMARY
    assert (await client.call_tool("wiki_random", {})).structured_content["title"] == "تهران"


async def test_redirect_off_host_refused(client, api):
    api["/api/rest_v1/page/random/summary"] = lambda r: httpx.Response(303, headers={"location": "https://evil.test/x"})
    api["/x"] = {}
    assert (await client.call_tool("wiki_random", {})).is_error


async def test_trending_falls_back_a_day(client, api):
    feed = {
        "mostread": {
            "date": "2026-11-16Z",
            "articles": [
                {"title": "A_b", "titles": {"normalized": "A b"}, "views": 5, "namespace": {"id": 0}},
                {"title": "ویژه:x", "views": 9, "namespace": {"id": -1}},
            ],
        },
        "image": {"title": "File:P.jpg", "image": {"source": "https://u/p.jpg"}, "description": {"text": "d"}},
    }
    api["/api/rest_v1/feed/featured/2026/11/17"] = feed
    r = (await client.call_tool("wiki_trending", {"date": "2026-11-17"})).structured_content
    assert r["date"] == "2026-11-16" and [a["title"] for a in r["most_read"]] == ["A b"]
    assert r["picture_of_the_day"]["title"] == "P.jpg"
    assert (await client.call_tool("wiki_trending", {"date": "bad"})).is_error


async def test_trending_default_day_falls_back_on_404(client, api):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"mostread": {"date": "2026-11-15Z", "articles": []}})

    for n in (1, 2, 3):
        api[f"/api/rest_v1/feed/featured/{datetime.now(timezone.utc) - timedelta(days=n):%Y/%m/%d}"] = handler
    r = (await client.call_tool("wiki_trending", {})).structured_content
    assert len(calls) == 2 and r["date"] == "2026-11-15"


async def test_nearby(client, api):
    api["/w/api.php"] = {"query": {"geosearch": [{"title": "T", "dist": 73.8, "lat": 1.0, "lon": 2.0}]}}
    r = (await client.call_tool("wiki_nearby", {"lat": 35.6, "lon": 51.3})).structured_content
    assert r["articles"][0]["distance_m"] == 74
    assert api.calls[0].url.params["gscoord"] == "35.6|51.3"


async def test_api_error_body(client, api):
    api["/w/api.php"] = {"error": {"code": "badvalue", "info": "Bad value."}}
    r = await client.call_tool("wiki_nearby", {"lat": 1, "lon": 1})
    assert r.is_error and "Bad value" in r.content[0].text


INFOBOX = (
    '<div><table class="infobox vcard"><tbody>'
    '<tr><th colspan="2">تهران</th></tr>'
    '<tr><td colspan="2"><style>.x{a:b}</style>تصویر</td></tr>'
    '<tr><th>کشور</th><td>ایران<sup class="reference">[۱]</sup></td></tr>'
    '<tr><th colspan="2">مساحت</th></tr>'
    "<tr><th>&#160;•&#160;شهری</th><td>۶۱۵&#160;km</td></tr>"
    '<tr><th colspan="2">جمعیت</th></tr>'
    "<tr><th>&#160;•&#160;شهری</th><td>۹ میلیون<br>برآورد</td></tr>"
    '<tr><th>مختصات</th><td>۳۵° <span class="geo-nondefault"><span>35.7</span></span>شمالی'
    "<table><tr><th>x</th><td>nested</td></tr></table></td></tr>"
    "</tbody></table><table><tr><th>other</th><td>no</td></tr></table></div>"
)


async def test_infobox_rows_groups_and_categories(client, api):
    def handler(request):
        if request.url.params["action"] == "parse":
            return httpx.Response(200, json={"parse": {"title": "تهران", "text": INFOBOX}})
        cats = [{"title": f"رده:c{i}"} for i in range(45)]
        return httpx.Response(200, json={"query": {"pages": [{"title": "تهران", "categories": cats}]}})

    api["/w/api.php"] = handler
    r = (await client.call_tool("wiki_infobox", {"title": "تهران"})).structured_content
    assert r["infobox_title"] == "تهران"
    assert r["fields"]["کشور"] == "ایران"
    assert r["fields"]["مساحت: شهری"] == "۶۱۵ km"
    assert r["fields"]["جمعیت: شهری"] == "۹ میلیون; برآورد"
    assert r["fields"]["مختصات"].startswith("۳۵° شمالی") and "35.7" not in r["fields"]["مختصات"]
    assert "other" not in r["fields"]
    assert len(r["categories"]) == 40 and r["categories_total"] == 45 and r["categories"][0] == "c0"


async def test_infobox_missing_title_hint(client, api):
    api["/w/api.php"] = {"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}}
    r = await client.call_tool("wiki_infobox", {"title": "x"})
    assert r.is_error and "wiki_search" in r.content[0].text


async def test_blank_title_is_a_clean_error(client, api):
    for name in ("wiki_summary", "wiki_article", "wiki_sections", "wiki_languages", "wiki_infobox"):
        r = await client.call_tool(name, {"title": "  "})
        assert r.is_error and "empty" in r.content[0].text, name
    assert not api.calls


async def test_section_matches_zwnj_and_arabic_letters(client, api):
    head = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 \u06a9\u062a\u0627\u0628"
    extract = f"Lead\n\n\n\n== {head} ==\nBody.\n== Two ==\nT."
    api["/w/api.php"] = {"query": {"pages": [{"title": "X", "extract": extract}]}}
    for s in (
        "\u0645\u06cc \u062e\u0648\u0627\u0647\u0645 \u06a9\u062a\u0627\u0628",
        "\u0645\u064a\u200c\u062e\u0648\u0627\u0647\u0645 \u0643\u062a\u0627\u0628",
    ):
        r = (await client.call_tool("wiki_article", {"title": "X", "section": s})).structured_content
        assert r["text"].startswith(f"== {head} ==") and "T." not in r["text"], s
    full = (await client.call_tool("wiki_article", {"title": "X"})).structured_content
    assert "\n\n\n" not in full["text"]


async def test_random_is_not_cached(client, api):
    titles = iter(["A", "B"])
    api["/api/rest_v1/page/random/summary"] = lambda r: httpx.Response(200, json={**SUMMARY, "title": next(titles)})
    first = (await client.call_tool("wiki_random", {})).structured_content["title"]
    second = (await client.call_tool("wiki_random", {})).structured_content["title"]
    assert (first, second) == ("A", "B")


async def test_trending_unknown_date_message(client, api):
    r = await client.call_tool("wiki_trending", {"date": "1999-01-01"})
    assert r.is_error and "No most-read data" in r.content[0].text
