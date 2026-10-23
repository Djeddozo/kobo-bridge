"""Discover: shelves of free books and articles for the Kobo, gathered and cached on the Pi.

Popular, New and Categories come from Standard Ebooks and Project Gutenberg; Articles come from the
person's own feeds in sources.json. Shelves are cached for hours so the catalogs see one request per
shelf, not one per tap; covers are shrunk to small grayscale JPEGs for e-ink.
"""
import hashlib
import io
import json
import logging
import re
import threading
import time
from email.utils import parsedate_to_datetime

from lxml import etree
from PIL import Image

from . import books, config, convert

log = logging.getLogger("kobo-bridge.discover")

SHELF_TTL = 6 * 3600
ARTICLE_TTL = 3600
PAGE_SIZE = 9  # a 3x3 cover grid on the Kobo
COVER_SIZE = (300, 450)
MAX_ARTICLES = 45
CATEGORIES = [  # Standard Ebooks subjects
    ("adventure", "Adventure"), ("mystery", "Mystery"), ("science-fiction", "Science Fiction"),
    ("fantasy", "Fantasy"), ("horror", "Horror"), ("comedy", "Comedy"), ("satire", "Satire"),
    ("drama", "Drama"), ("poetry", "Poetry"), ("shorts", "Short stories"), ("childrens", "Children's"),
    ("philosophy", "Philosophy"), ("spirituality", "Spirituality"), ("travel", "Travel"),
    ("biography", "Biography"), ("autobiography", "Autobiography"), ("memoir", "Memoir"),
    ("nonfiction", "Nonfiction"), ("fiction", "Fiction"),
]
CATEGORY_NAMES = dict(CATEGORIES)
DEFAULT_SOURCES = {
    "articles": [
        {"name": "The Micro Philosopher", "feed": "https://themicrophilosopher.com/feed"},
        {"name": "Aeon", "feed": "https://aeon.co/feed.rss"},
    ]
}


def title_key(title):
    return re.sub(r"[^a-z0-9]", "", (title or "").lower().split(";")[0].split(":")[0])


def cover_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def shrink_cover(data):
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert("L")
    im.thumbnail(COVER_SIZE)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=75, optimize=True)
    return out.getvalue()


def parse_feed(data, source_name):
    """Items from an RSS or Atom feed: title, link, date (epoch) and the source's name."""
    root = convert.parse_xml(data)
    items = []
    for node in root.iter():
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag not in ("item", "entry"):
            continue
        children = {etree.QName(c).localname: c for c in node if isinstance(c.tag, str)}
        title = " ".join((children["title"].text or "").split()) if "title" in children else ""
        link = None
        for c in node:
            if isinstance(c.tag, str) and etree.QName(c).localname == "link":
                link = c.get("href") or (c.text or "").strip()
                if c.get("rel") in (None, "alternate"):
                    break
        when = 0.0
        for name in ("pubDate", "published", "updated", "date"):
            if name in children and children[name].text:
                text = children[name].text.strip()
                try:
                    when = parsedate_to_datetime(text).timestamp()
                except (TypeError, ValueError):
                    try:
                        when = time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
                    except ValueError:
                        pass
                break
        if title and link and link.startswith(("http://", "https://")):
            items.append({"kind": "article", "title": title, "url": link, "source": source_name, "date": when})
    return items


class Discover:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}        # shelf name -> (fetched at, items)
        self.covers = {}       # cover id -> original URL
        self.cover_dir = config.DATA_DIR / "covers"
        self.cover_dir.mkdir(parents=True, exist_ok=True)

    # --- shelves ---------------------------------------------------------------------------------------

    def tabs(self):
        return {"tabs": [{"id": "popular", "name": "Popular"}, {"id": "new", "name": "New"},
                         {"id": "categories", "name": "Categories"}, {"id": "articles", "name": "Articles"}],
                "categories": [{"id": f"cat-{key}", "name": name} for key, name in CATEGORIES]}

    def shelf(self, name, page=1):
        if name == "articles":
            items, title = self._cached(name, ARTICLE_TTL, self._articles), "Articles"
        elif name == "popular":
            items, title = self._cached(name, SHELF_TTL, self._popular), "Popular"
        elif name == "new":
            items, title = self._cached(name, SHELF_TTL, self._new), "New at Standard Ebooks"
        elif name.startswith("cat-") and name[4:] in CATEGORY_NAMES:
            key = name[4:]
            items = self._cached(name, SHELF_TTL, lambda: books.standard_list(
                f"tags%5B%5D={key}&sort=popularity&per-page=48", 48))
            title = CATEGORY_NAMES[key]
        else:
            raise KeyError(name)
        pages = max(1, -(-len(items) // PAGE_SIZE))
        page = min(max(1, page), pages)
        chunk = items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        return {"shelf": name, "title": title, "page": page, "pages": pages, "total": len(items),
                "items": [self._public(item) for item in chunk]}

    def _cached(self, name, ttl, build):
        with self.lock:
            hit = self.cache.get(name)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            items = build()
        except (convert.ConvertError, ValueError, etree.XMLSyntaxError, etree.ParserError) as exc:
            if hit:  # a catalog hiccup: keep showing the last good shelf
                log.warning("Shelf %s refresh failed, using cached copy: %s", name, exc)
                return hit[1]
            raise convert.ConvertError(f"Couldn't load {name} right now") from exc
        with self.lock:
            self.cache[name] = (time.time(), items)
        return items

    def _popular(self):
        """Standard Ebooks' most popular first (nicest editions), then Gutenberg's most downloaded."""
        items, seen = [], set()
        for finder in (lambda: books.standard_list("sort=popularity&per-page=24", 24),
                       lambda: books.gutenberg_list("sort_order=downloads", 25)):
            try:
                found = finder()
            except (convert.ConvertError, ValueError, etree.XMLSyntaxError, etree.ParserError):
                continue
            for item in found:
                key = title_key(item["title"])
                if key and key not in seen:
                    seen.add(key)
                    items.append(item)
        if not items:
            raise convert.ConvertError("Both catalogs are unreachable")
        return items

    def _new(self):
        return books.standard_list("sort=default&per-page=48", 48)

    def _articles(self):
        items = []
        for source in self.sources().get("articles", []):
            try:
                data = convert.fetch(source["feed"], accept="application/rss+xml,application/atom+xml,*/*;q=0.5")[2]
                items += parse_feed(data, source.get("name") or source["feed"])
            except (convert.ConvertError, ValueError, KeyError, etree.XMLSyntaxError) as exc:
                log.warning("Feed %s failed: %s", source.get("feed"), exc)
        items.sort(key=lambda i: i["date"], reverse=True)
        return items[:MAX_ARTICLES]

    def sources(self):
        try:
            return json.loads(config.SOURCES_FILE.read_text())
        except (OSError, ValueError):
            return DEFAULT_SOURCES

    def _public(self, item):
        out = {k: item.get(k) for k in ("kind", "title", "author", "source", "url", "date")}
        out["kind"] = item.get("kind") or "book"
        if item.get("cover"):
            cid = cover_id(item["cover"])
            self.covers[cid] = item["cover"]
            out["cover"] = cid
        return out

    # --- covers ----------------------------------------------------------------------------------------

    def cover_path(self, cid):
        """A shrunk grayscale cover, fetched on first use and kept on disk; None if unknown or unavailable."""
        if not re.fullmatch(r"[0-9a-f]{20}", cid or ""):
            return None
        path = self.cover_dir / f"{cid}.jpg"
        if path.exists():
            return path
        url = self.covers.get(cid)
        if not url:
            return None
        try:
            data = convert.fetch(url, accept="image/jpeg,image/*;q=0.8")[2]
            path.write_bytes(shrink_cover(data))
        except Exception as exc:  # a missing cover just shows a placeholder on the Kobo
            log.info("Cover %s unavailable: %s", url, exc)
            return None
        return path
