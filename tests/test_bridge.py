import time
import zipfile

import pytest
from ebooklib import epub
from fastapi.testclient import TestClient

from bridge import config, convert
from bridge.security import find_url, public_host, safe_filename, safe_url

ARTICLE = b"""<html><head><title>Test Article</title><meta name="author" content="Ada Writer"></head>
<body><nav>menu menu</nav><article><h1>Test Article</h1>
<p>This is the first paragraph of a long enough article so that the extractor keeps it as main content.</p>
<p>Second paragraph with <b>bold</b> text &amp; an ampersand, plus more words to look like a real article body.</p>
<p>Third paragraph, because readability tools like a bit of substance before they trust a page.</p>
</article><footer>footer links</footer></body></html>"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "library" / "Kobo Bridge")
    return tmp_path


@pytest.fixture
def client(env):
    from bridge.main import create_app
    with TestClient(create_app(), base_url="http://127.0.0.1:8850") as c:
        yield c


def make_epub(path, title="Small Book"):
    book = epub.EpubBook()
    book.set_identifier("test-book")
    book.set_title(title)
    book.set_language("en")
    ch = epub.EpubHtml(title="One", file_name="one.xhtml", lang="en")
    ch.content = "<html><body><h1>One</h1><p>Hello Kobo.</p></body></html>"
    book.add_item(ch)
    book.toc = [ch]
    book.spine = [ch]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)
    return path


def wait_for(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = next(j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id)
        if job["status"] in {"done", "failed"}:
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_urls_and_names():
    assert safe_url("https://example.com/a") == "https://example.com/a"
    for bad in ["ftp://x", "https://user:pw@example.com", "javascript:alert(1)", ""]:
        with pytest.raises(ValueError):
            safe_url(bad)
    assert find_url(None, "Look at this https://example.com/story?id=1).") == "https://example.com/story?id=1"
    assert safe_filename("../../etc/passwd") == "etc passwd"
    assert safe_filename('a<b>:c"?.epub') == "abc.epub"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "192.168.1.10", "100.64.0.7", "10.0.0.1"])
def test_private_hosts_refused(host):
    with pytest.raises(ValueError):
        public_host(host)


def test_unknown_host_and_cross_origin_refused(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421
    assert client.post("/api/links", json={"url": "https://example.com"}).status_code == 403  # no custom header
    r = client.post("/api/links", json={"url": "https://example.com"},
                    headers={"x-kobo-bridge": "1", "origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/share", data={"text": "https://example.com"}, headers={"origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_pages_and_manifest(client):
    assert "Kobo Bridge" in client.get("/").text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["share_target"]["action"] == "/share"
    assert client.get("/sw.js").status_code == 200
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]


def test_upload_epub_becomes_kepub(client, env):
    src = make_epub(env / "Small Book.epub")
    with open(src, "rb") as fh:
        r = client.post("/api/files", files={"file": ("Small Book.epub", fh, "application/epub+zip")},
                        headers={"x-kobo-bridge": "1"})
    assert r.status_code == 200, r.text
    job = wait_for(client, r.json()["ids"][0])
    assert job["status"] == "done", job
    assert job["output"] == "Small Book.kepub.epub"
    assert zipfile.is_zipfile(config.OUTPUT_DIR / job["output"])
    assert not any((config.DATA_DIR / "uploads").iterdir())  # upload cleaned up


def test_downloaded_epub_named_from_metadata(env):
    src = make_epub(env / "pg1342.epub", title="Pride and Prejudice")
    assert convert.epub_title(src) == "Pride and Prejudice"
    workdir = env / "work"
    workdir.mkdir()
    title, output = convert.process_file(src, "pg1342.epub", workdir)
    assert output == "Pride and Prejudice.kepub.epub"
    assert convert.epub_title(env / "missing.epub") is None


def test_refused_fetch_retries_as_browser(monkeypatch):
    agents = []

    def fake_fetch_as(url, agent, accept=None):
        agents.append(agent)
        if len(agents) == 1:
            raise convert.Refused("403")
        return url, "text/html", b"<html></html>", None

    monkeypatch.setattr(convert, "fetch_as", fake_fetch_as)
    assert convert.fetch("https://example.com")[1] == "text/html"
    assert agents == list(config.USER_AGENTS)


def test_unsupported_upload_rejected(client):
    r = client.post("/api/files", files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
                    headers={"x-kobo-bridge": "1"})
    assert r.status_code == 400


def test_share_text_link_and_pdf_file(client, monkeypatch):
    seen = []
    monkeypatch.setattr(convert, "process_url", lambda url, title, workdir: (seen.append(url), ("T", "t.epub"))[1])
    r = client.post("/share", data={"title": "Story", "text": "Read this: https://example.com/story"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?shared=1"
    r = client.post("/share", files={"file": ("doc.pdf", b"%PDF-1.4 test", "application/pdf")}, follow_redirects=False)
    assert r.headers["location"] == "/?shared=1"
    r = client.post("/share", data={"text": "no link here"}, follow_redirects=False)
    assert r.headers["location"] == "/?shared=0"
    jobs = client.get("/api/jobs").json()["jobs"]
    for job in jobs:
        wait_for(client, job["id"])
    assert seen == ["https://example.com/story"]
    assert (config.OUTPUT_DIR / "doc.pdf").read_bytes() == b"%PDF-1.4 test"


def test_article_to_epub(env):
    workdir = env / "work"
    workdir.mkdir()
    config.OUTPUT_DIR.mkdir(parents=True)
    title, output = convert.article_to_epub(ARTICLE, "https://example.com/test", None, workdir)
    assert title == "Test Article - Ada Writer"
    assert output == "Test Article - Ada Writer.kepub.epub"
    with zipfile.ZipFile(config.OUTPUT_DIR / output) as z:
        text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith("html"))
    assert "first paragraph" in text and "&amp;" in text
    assert "footer links" not in text
    assert "intentionally left blank" not in text.lower()  # no kepubify dummy title page


def png(size, mode="RGBA", color=(200, 30, 30, 255)):
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


def test_shrink_image():
    import io
    from PIL import Image
    jpeg = convert.shrink_image(png((3000, 2000)))
    im = Image.open(io.BytesIO(jpeg))
    assert im.format == "JPEG" and im.mode == "L"          # grayscale for e-ink
    assert im.size[0] <= 1264 and im.size[1] <= 1680       # fits the Libra's screen
    assert convert.shrink_image(png((96, 96))) is None      # avatar / icon
    assert convert.shrink_image(png((1200, 60))) is None    # banner or divider
    assert convert.shrink_image(b"not an image") is None


ARTICLE_WITH_PICTURES = b"""<html><head><title>Lighthouses</title></head><body>
<header><img src="/static/site-logo.png" alt="Site logo"></header>
<article class="typography newsletter-post post"><h1>Lighthouses</h1>
<p>Lighthouses have guided ships for centuries, and this paragraph is long enough to count as the article body.</p>
<figure><img src="/media/photo.png" alt="A lighthouse at dusk"></figure>
<p>Keepers lived in them for months at a time, which this second paragraph explains in some more detail here.</p>
<p><img src="/media/tiny.png" alt=""> A final paragraph with more substance so the extractor trusts the page.</p>
<figure><img src="/media/photo.png" alt="A lighthouse at dusk"></figure>
<p><img src="/img/author-avatar.jpg" alt="Author avatar"></p>
<div class="digestPostEmbed-flwiST"><a href="/p/other"><img src="/media/other-post-cover.png" alt="Another post">
<span>How To Make Life Interesting Again</span></a></div>
</article></body></html>"""


def test_article_keeps_only_its_own_pictures(env, monkeypatch):
    fetched = []
    images = {"https://example.com/media/photo.png": png((1600, 1000)),
              "https://example.com/media/tiny.png": png((40, 40)),
              "https://example.com/static/site-logo.png": png((800, 400)),
              "https://example.com/img/author-avatar.jpg": png((400, 400)),
              "https://example.com/media/other-post-cover.png": png((1200, 600))}

    def fake_fetch(url, accept=None):
        fetched.append(url)
        return url, "image/png", images[url], None

    monkeypatch.setattr(convert, "fetch", fake_fetch)
    workdir = env / "work"
    workdir.mkdir()
    title, output = convert.article_to_epub(ARTICLE_WITH_PICTURES, "https://example.com/story", None, workdir)
    with zipfile.ZipFile(config.OUTPUT_DIR / output) as z:
        pictures = [n for n in z.namelist() if n.endswith(".jpg")]
        text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith("html"))
    assert len(pictures) == 1 and pictures[0].endswith("images/img01.jpg")
    assert 'src="images/img01.jpg"' in text and "A lighthouse at dusk" in text
    assert "logo" not in " ".join(fetched) and "avatar" not in " ".join(fetched)  # never even downloaded
    assert fetched.count("https://example.com/media/photo.png") == 1                # duplicate fetched once
    assert "example.com/media" not in text                                         # no remote links left
    assert "other-post-cover" not in " ".join(fetched)                             # embedded card for another post
    assert "Make Life Interesting" not in text


def test_wikipedia_thumbnails_get_sharper():
    thumb = "https://upload.wikimedia.org/wikipedia/commons/thumb/d/df/Kindle.png/250px-Kindle.png?utm_source=x"
    assert convert.larger_version(thumb) == "https://upload.wikimedia.org/wikipedia/commons/thumb/d/df/Kindle.png/960px-Kindle.png"
    assert convert.larger_version("https://example.com/250px-photo.jpg") is None


def test_restart_recovery(env):
    from bridge.jobs import JobStore
    config.DATA_DIR.mkdir(parents=True)
    store = JobStore(config.DATA_DIR / "jobs.sqlite3")
    running = store.add("url", "https://example.com/a")
    store.update(running, status="running")
    queued = store.add("url", "https://example.com/b")
    assert store.recover() == [queued]
    assert store.get(running)["status"] == "failed"
