import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_search_and_summary(client):
    r = (await client.call_tool("wiki_search", {"query": "تهران", "limit": 3})).structured_content
    assert r["total"] > 1000 and r["results"][0]["title"] == "تهران"
    s = (await client.call_tool("wiki_summary", {"title": "تهران"})).structured_content
    assert "پایتخت" in s["extract"] and s["wikidata"] == "Q3616" and s["lat"]


async def test_live_article_section(client):
    r = (
        await client.call_tool("wiki_article", {"title": "تهران", "section": "نام", "max_chars": 500})
    ).structured_content
    assert r["text"].startswith("== نام ==") and r["truncated"] and len(r["text"]) == 500
    secs = (await client.call_tool("wiki_sections", {"title": "تهران"})).structured_content["sections"]
    assert secs[0] == {"number": "1", "level": 1, "title": "نام"}


async def test_live_languages(client):
    r = (await client.call_tool("wiki_languages", {"title": "تهران", "lang": "en"})).structured_content
    assert r["languages"][0]["title"] == "Tehran"


async def test_live_random(client):
    assert (await client.call_tool("wiki_random", {})).structured_content["title"]


async def test_live_trending(client):
    r = (await client.call_tool("wiki_trending", {"limit": 5})).structured_content
    assert len(r["most_read"]) == 5 and r["most_read"][0]["views"] > 0


async def test_live_nearby(client):
    r = (await client.call_tool("wiki_nearby", {"lat": 35.6892, "lon": 51.389, "radius_m": 500})).structured_content
    assert r["articles"][0]["title"] == "تهران"


async def test_live_missing_article(client):
    assert (await client.call_tool("wiki_summary", {"title": "نبودنبودنبود۱۲۳"})).is_error


async def test_live_infobox(client):
    r = (await client.call_tool("wiki_infobox", {"title": "تهران"})).structured_content
    assert r["fields"]["کشور"] == "ایران" and r["fields"]["جمعیت: شهری"] and r["categories"]
