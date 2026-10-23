import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bridge import config, convert, discover


def se_listing(*books):
    items = "".join(
        f'<li typeof="schema:Book" about="/ebooks/{slug}"><img src="/images/covers/{slug.replace("/", "_")}/cover@2x.jpg"/>'
        f'<span property="schema:name">{title}</span><span property="schema:name">{author}</span></li>'
        for slug, title, author in books)
    return f"<html><body><ol>{items}</ol></body></html>".encode()


SE_POPULAR = se_listing(("mary-shelley/frankenstein", "Frankenstein", "Mary Shelley"),
                        ("jane-austen/pride-and-prejudice", "Pride and Prejudice", "Jane Austen"))
SE_NEW = se_listing(*[(f"author-{i}/book-{i}", f"New Book {i}", f"Author {i}") for i in range(1, 21)])
SE_ADVENTURE = se_listing(("robert-louis-stevenson/treasure-island", "Treasure Island", "Robert Louis Stevenson"))

PG_POPULAR = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>https://www.gutenberg.org/ebooks/1342.opds</id><title>Pride and Prejudice</title><content>Jane Austen</content></entry>
<entry><id>https://www.gutenberg.org/ebooks/2701.opds</id><title>Moby Dick; Or, The Whale</title><content>Herman Melville</content></entry>
</feed>"""

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Micro</title>
<item><title>Older essay</title><link>https://example.com/older</link><pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Newest essay</title><link>https://example.com/newest</link><pubDate>Sun, 18 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Aeon</title>
<entry><title>Middle essay</title><link rel="alternate" href="https://aeon.example/middle"/><updated>2026-10-15T09:00:00Z</updated></entry>
</feed>"""

XXE = b"""<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<rss><channel><item><title>&x;</title><link>https://example.com/x</link></item></channel></rss>"""


def jpeg(size=(600, 900)):
    out = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "library" / "Kobo Bridge")
    monkeypatch.setattr(config, "SOURCES_FILE", tmp_path / "sources.json")
    (tmp_path / "sources.json").write_text(json.dumps({"articles": [
        {"name": "Micro", "feed": "https://example.com/feed"},
        {"name": "Aeon", "feed": "https://aeon.example/feed"},
        {"name": "Broken", "feed": "https://broken.example/feed"}]}))
    return tmp_path


@pytest.fixture
def fetched(env, monkeypatch):
    calls = []

    def fake_fetch(url, accept=None):
        calls.append(url)
        if "standardebooks.org/ebooks?sort=popularity" in url:
            body = SE_POPULAR
        elif "standardebooks.org/ebooks?sort=default" in url:
            body = SE_NEW
        elif "standardebooks.org/ebooks?tags%5B%5D=adventure" in url:
            body = SE_ADVENTURE
        elif "gutenberg.org/ebooks/search.opds/?sort_order=downloads" in url:
            body = PG_POPULAR
        elif url == "https://example.com/feed":
            body = RSS
        elif url == "https://aeon.example/feed":
            body = ATOM
        elif url.endswith(".jpg"):
            body = jpeg()
        else:
            raise convert.ConvertError("The site answered 404")
        return url, "text/html", body, None

    monkeypatch.setattr(convert, "fetch", fake_fetch)
    return calls


def test_popular_merges_and_dedupes(fetched):
    shelf = discover.Discover().shelf("popular")
    titles = [i["title"] for i in shelf["items"]]
    assert titles == ["Frankenstein", "Pride and Prejudice", "Moby Dick; Or, The Whale"]  # Gutenberg's P&P dropped
    assert shelf["items"][0]["source"] == "Standard Ebooks" and shelf["items"][0]["kind"] == "book"
    assert len(shelf["items"][0]["cover"]) == 20  # an id, not the remote URL


def test_new_is_paged_in_threes_by_three(fetched):
    d = discover.Discover()
    first, third = d.shelf("new", 1), d.shelf("new", 3)
    assert first["pages"] == 3 and first["total"] == 20 and len(first["items"]) == 9
    assert [i["title"] for i in third["items"]] == ["New Book 19", "New Book 20"]
    assert d.shelf("new", 99)["page"] == 3
    assert sum("sort=default" in u for u in fetched) == 1  # cached: the catalog is asked once


def test_categories(fetched):
    d = discover.Discover()
    assert {"id": "cat-adventure", "name": "Adventure"} in d.tabs()["categories"]
    shelf = d.shelf("cat-adventure")
    assert shelf["title"] == "Adventure" and shelf["items"][0]["title"] == "Treasure Island"
    with pytest.raises(KeyError):
        d.shelf("cat-not-a-subject")


def test_articles_newest_first_and_broken_feeds_skipped(fetched):
    items = discover.Discover().shelf("articles")["items"]
    assert [(i["title"], i["source"]) for i in items] == [
        ("Newest essay", "Micro"), ("Middle essay", "Aeon"), ("Older essay", "Micro")]
    assert items[0]["kind"] == "article" and items[1]["url"] == "https://aeon.example/middle"


def test_feeds_cannot_read_local_files():
    items = discover.parse_feed(XXE, "Evil")
    assert all("root:" not in i["title"] for i in items)


def test_last_good_shelf_survives_an_outage(fetched, monkeypatch):
    d = discover.Discover()
    d.shelf("new")
    d.cache["new"] = (time.time() - discover.SHELF_TTL - 1, d.cache["new"][1])  # expired
    monkeypatch.setattr(convert, "fetch", lambda url, accept=None: (_ for _ in ()).throw(convert.ConvertError("down")))
    assert d.shelf("new")["total"] == 20


def test_covers_are_shrunk_and_cached(fetched):
    d = discover.Discover()
    cid = d.shelf("popular")["items"][0]["cover"]
    path = d.cover_path(cid)
    im = Image.open(path)
    assert im.mode == "L" and im.size[0] <= 300 and im.size[1] <= 450
    jpg_calls = sum(u.endswith(".jpg") for u in fetched)
    assert d.cover_path(cid) == path and sum(u.endswith(".jpg") for u in fetched) == jpg_calls  # from disk
    assert d.cover_path("../../etc/passwd") is None and d.cover_path("0" * 20) is None


@pytest.fixture
def client(env, fetched):
    from bridge.main import create_app
    with TestClient(create_app(), base_url="http://127.0.0.1:8850") as c:
        c.headers.update({"x-kobo-bridge": "1",
                          "authorization": f"Bearer {(config.DATA_DIR / 'kobo-token').read_text()}"})
        yield c


def test_discover_endpoints(client, monkeypatch):
    assert client.get("/api/discover", headers={"authorization": ""}).status_code == 401
    assert [t["id"] for t in client.get("/api/discover").json()["tabs"]] == ["popular", "new", "categories", "articles"]
    shelf = client.get("/api/discover/shelf/new?page=2").json()
    assert shelf["page"] == 2 and len(shelf["items"]) == 9
    assert client.get("/api/discover/shelf/nope").status_code == 404
    cover = client.get(f"/api/discover/cover/{shelf['items'][0]['cover']}")
    assert cover.status_code == 200 and cover.headers["content-type"] == "image/jpeg"
    assert client.get("/api/discover/cover/" + "f" * 20).status_code == 404

    monkeypatch.setattr(convert, "process_url", lambda url, title, workdir, outdir=None: (title, f"{title}.kepub.epub"))
    r = client.post("/api/discover/article", json={"url": "https://example.com/newest", "title": "Newest essay"})
    assert r.json()["folder"] == "Kobo Bridge"
    for _ in range(100):
        job = client.get(f"/api/books/job/{r.json()['id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["output"] == "Newest essay.kepub.epub"
