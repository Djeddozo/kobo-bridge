import time

import pytest
from fastapi.testclient import TestClient

from bridge import books, claude, config, convert

STANDARD_SEARCH = b"""<html><body><ol>
<li typeof="schema:Book" about="/ebooks/jules-verne/robur-the-conqueror/sampson-low">
  <a href="/ebooks/jules-verne/robur-the-conqueror/sampson-low"><span property="schema:name">Robur the Conqueror</span></a>
  <p property="schema:author" typeof="schema:Person"><span property="schema:name">Jules Verne</span></p></li>
<li typeof="schema:Book" about="/ebooks/jules-verne/the-giant-raft/w-j-gordon">
  <span property="schema:name">The Giant Raft</span><span property="schema:name">Jules Verne</span></li>
</ol></body></html>"""

GUTENBERG_SEARCH = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>https://www.gutenberg.org/ebooks/authors/search.opds/?query=verne</id><title>Authors</title></entry>
  <entry><id>https://www.gutenberg.org/ebooks/103.opds</id><title>Around the World in
    Eighty Days</title><content type="text">Jules Verne</content></entry>
  <entry><id>https://www.gutenberg.org/ebooks/164.opds</id><title>Twenty Thousand Leagues under the Sea</title>
    <content type="text">Jules Verne</content></entry>
  <entry><id>https://www.gutenberg.org/ebooks/999.opds</id><title>Verne's Voyages</title>
    <content type="text">161 downloads</content></entry>
  <entry><id>https://www.gutenberg.org/ebooks/555.opds</id><title>I Love You, California</title>
    <content type="text">161 downloads</content></entry>
</feed>"""

STANDARD_PAGE = b"""<html><body>
<a href="/ebooks/jules-verne/robur-the-conqueror/sampson-low/downloads/robur.azw3">Kindle</a>
<a href="/ebooks/jules-verne/robur-the-conqueror/sampson-low/downloads/robur.kepub.epub">Kobo</a>
<a href="/ebooks/jules-verne/robur-the-conqueror/sampson-low/downloads/robur_advanced.epub">Advanced</a>
<a href="/ebooks/jules-verne/robur-the-conqueror/sampson-low/downloads/robur.epub">Compatible epub</a>
</body></html>"""


@pytest.fixture
def pages(monkeypatch):
    fetched = []

    def fake_fetch(url, accept=None):
        fetched.append(url)
        if url.startswith("https://standardebooks.org/ebooks?query="):
            return url, "application/xhtml+xml", STANDARD_SEARCH, None
        if url.startswith("https://www.gutenberg.org/ebooks/search.opds/"):
            return url, "application/atom+xml", GUTENBERG_SEARCH, None
        if url == "https://standardebooks.org/ebooks/jules-verne/robur-the-conqueror/sampson-low":
            return url, "application/xhtml+xml", STANDARD_PAGE, None
        raise convert.ConvertError("The site answered 404")

    monkeypatch.setattr(convert, "fetch", fake_fetch)
    return fetched


def test_search_combines_sources(pages):
    results = books.search("  jules   verne ")
    assert [r["source"] for r in results] == ["Standard Ebooks"] * 2 + ["Project Gutenberg"] * 3
    assert results[-1] == {"source": "Project Gutenberg", "title": "Verne's Voyages", "author": None,
                           "url": "https://www.gutenberg.org/ebooks/999",  # "161 downloads" isn't an author
                           "cover": "https://www.gutenberg.org/cache/epub/999/pg999.cover.medium.jpg"}
    assert "I Love You, California" not in [r["title"] for r in results]   # unrelated feed filler
    assert results[0] == {"source": "Standard Ebooks", "title": "Robur the Conqueror", "author": "Jules Verne",
                          "url": "https://standardebooks.org/ebooks/jules-verne/robur-the-conqueror/sampson-low",
                          "cover": None}  # this sample listing has no cover image
    assert results[2]["title"] == "Around the World in Eighty Days"   # whitespace tidied
    assert results[2]["url"] == "https://www.gutenberg.org/ebooks/103"
    assert "query=jules+verne" in pages[0]
    assert books.search("   ") == []


def test_search_survives_a_failing_source(pages, monkeypatch):
    real = convert.fetch

    def flaky(url, accept=None):
        if "standardebooks" in url:
            raise convert.ConvertError("The site answered 503")
        return real(url, accept)

    monkeypatch.setattr(convert, "fetch", flaky)
    assert [r["source"] for r in books.search("verne")] == ["Project Gutenberg"] * 3


def test_download_urls(pages):
    assert books.download_url("https://www.gutenberg.org/ebooks/164") == "https://www.gutenberg.org/ebooks/164.epub3.images"
    assert (books.download_url("https://standardebooks.org/ebooks/jules-verne/robur-the-conqueror/sampson-low")
            == "https://standardebooks.org/ebooks/jules-verne/robur-the-conqueror/sampson-low/downloads/robur.epub"
               "?source=download")
    for bad in ["https://annas-archive.org/md5/abc", "https://libgen.is/book/index.php?md5=x",
                "https://www.gutenberg.org/ebooks/164/../../etc", "http://www.gutenberg.org/ebooks/164"]:
        assert not books.is_book_page(bad)
        with pytest.raises(convert.ConvertError):
            books.download_url(bad)


def test_fetch_book_never_turns_an_error_page_into_a_book(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "fetch", lambda url, accept=None: (url, "text/html", b"<html>Your Download Has Started!</html>", None))
    with pytest.raises(convert.ConvertError, match="didn't send an ebook"):
        books.fetch_book("https://www.gutenberg.org/ebooks/164", "x", tmp_path, tmp_path / "Free books")
    assert not (tmp_path / "Free books").exists()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "library" / "Kobo Bridge")
    monkeypatch.setattr(config, "BOOKS_OUTPUT_DIR", tmp_path / "library" / "Free books")
    from bridge.main import create_app
    with TestClient(create_app(), base_url="http://127.0.0.1:8850") as c:
        c.headers.update({"x-kobo-bridge": "1",
                          "authorization": f"Bearer {(config.DATA_DIR / 'kobo-token').read_text()}"})
        yield c


def test_book_endpoints(client, pages, monkeypatch):
    assert client.get("/api/books/search?q=verne", headers={"authorization": ""}).status_code == 401
    assert len(client.get("/api/books/search?q=verne").json()["results"]) == 5

    r = client.post("/api/books/get", json={"url": "https://annas-archive.org/md5/abc", "title": "x"})
    assert r.status_code == 400

    picked = []

    def fake_fetch_book(page_url, title, workdir, outdir):
        picked.append((page_url, title, outdir.name))
        return title, f"{title}.kepub.epub"

    monkeypatch.setattr(books, "fetch_book", fake_fetch_book)
    r = client.post("/api/books/get", json={"url": "https://www.gutenberg.org/ebooks/164",
                                            "title": "Twenty Thousand Leagues"})
    assert r.json()["folder"] == "Free books"
    job_id = r.json()["id"]
    for _ in range(100):
        job = client.get(f"/api/books/job/{job_id}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["output"] == "Twenty Thousand Leagues.kepub.epub"
    assert picked == [("https://www.gutenberg.org/ebooks/164", "Twenty Thousand Leagues", "Free books")]


def test_claude_book_suggestions(client, monkeypatch):
    def fake_run(prompt, workspace, session_id=None):
        (workspace / "out" / "suggestions.txt").write_text(
            "- Twenty Thousand Leagues under the Sea - Jules Verne\n\n* The Time Machine - H. G. Wells\n")
        return {"result": "Two classic adventures you can get for free.", "session_id": "s1"}

    monkeypatch.setattr(claude, "run_claude", fake_run)
    ask_id = client.post("/api/claude", json={"prompt": "Recommend me sci-fi classics"}).json()["id"]
    for _ in range(100):
        item = client.get(f"/api/claude/{ask_id}").json()
        if item["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert item["suggestions"] == ["Twenty Thousand Leagues under the Sea - Jules Verne",
                                   "The Time Machine - H. G. Wells"]
    assert item["files"] == []  # the suggestions list is not turned into an ebook
