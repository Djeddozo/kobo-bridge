"""Turn shared links and files into books in the Kobo library folder."""
import html
import io
import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from email.message import Message
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

import httpx
import trafilatura
from ebooklib import epub
from lxml import etree
from lxml import html as lhtml
from PIL import Image, ImageOps

from . import config
from .security import public_host, safe_filename, safe_url

# KOReader opens these as they are; EPUBs are converted to KEPUB for the stock Kobo reader.
PASSTHROUGH = {".pdf", ".djvu", ".cbz", ".cbt", ".fb2", ".mobi", ".txt", ".rtf", ".doc", ".docx",
               ".odt", ".htm", ".html", ".chm", ".xps", ".md"}
SUPPORTED = PASSTHROUGH | {".epub"}
CONTENT_TYPES = {
    "application/epub+zip": ".epub",
    "application/pdf": ".pdf",
    "application/x-mobipocket-ebook": ".mobi",
    "application/vnd.comicbook+zip": ".cbz",
    "application/x-cbz": ".cbz",
    "image/vnd.djvu": ".djvu",
    "application/x-fictionbook+xml": ".fb2",
}

ARTICLE_CSS = """
body { font-family: serif; line-height: 1.45; }
h1 { font-size: 1.5em; margin: 0 0 .3em; }
p.meta { font-size: .8em; color: #555; margin: 0 0 .2em; }
hr { margin: 1em 0; }
blockquote { margin: .5em 1.5em; font-style: italic; }
img { display: block; max-width: 100%; height: auto; margin: .6em auto; }
"""

log = logging.getLogger("kobo-bridge.convert")

# For XML from other sites (feeds, catalogs): no entity expansion, no network or local file access.
SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def parse_xml(data):
    return etree.fromstring(data, parser=SAFE_XML)

# Article pictures: only those inside the article body (the extractor drops page furniture), minus anything
# that looks like a logo, icon or avatar, too small, or a banner-shaped strip.
NOT_CONTENT = re.compile(r"(?<![a-z])(logo|icon|icons|avatar|sprite|badge|emoji|spacer|pixel|tracking|tracker|"
                         r"gravatar|placeholder|loading|blank|transparent|advert|ads?)(?![a-z])", re.I)
# Blocks inside an article that point elsewhere: embedded cards for other posts, "related"/"recommended"
# lists, newsletter sign-ups and share bars. Their pictures and text aren't part of the article.
NOT_ARTICLE = re.compile(r"(post-?embed|embedded-?post|digest-?post|related|recommend|read-?next|more-?from|"
                         r"promo|newsletter-?(signup|form|cta|box|widget)|subscribe|subscription|signup|"
                         r"share-?(bar|buttons|tools)|social)", re.I)
# Such blocks are small; anything bigger (or an <article>/<main>) is the article itself, whatever its class.
NOT_ARTICLE_MAX_WORDS = 300
WIKIMEDIA_THUMB = re.compile(r"^(https://(?:upload|thumb)\.wikimedia\.org/.+/thumb/.+/)(\d+)px-([^/]+)$")
MAX_IMAGES = 25
MIN_IMAGE_SIDE = 150
MAX_IMAGE_RATIO = 5
IMAGE_BUDGET_SECONDS = 120
SCREEN_SIZE = (1264, 1680)  # Libra H2O
IMAGE_ACCEPT = "image/jpeg,image/png,image/webp,image/gif;q=0.9,*/*;q=0.5"
Image.MAX_IMAGE_PIXELS = 40_000_000


class ConvertError(Exception):
    """A problem worth showing to the user as it is."""


def suffix_of(name):
    name = name.lower()
    return ".epub" if name.endswith(".kepub.epub") else PurePosixPath(name).suffix


def stem_of(name):
    name = PurePosixPath(name).name
    lower = name.lower()
    for ending in (".kepub.epub", suffix_of(name)):
        if ending and lower.endswith(ending):
            return name[: -len(ending)]
    return name


def unique_path(directory, stem, suffix):
    path, n = directory / f"{stem}{suffix}", 2
    while path.exists():
        path, n = directory / f"{stem} ({n}){suffix}", n + 1
    return path


def publish(tmp, stem, suffix, outdir=None):
    """Move a finished file into the library in one step, so Syncthing never sees half a book."""
    outdir = outdir or config.OUTPUT_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    out = unique_path(outdir, safe_filename(stem), suffix)
    shutil.move(str(tmp), out)
    return out.name


def kepubify(src, workdir, generated=False):
    """Returns the KEPUB, or None if kepubify can't handle this EPUB (KOReader still reads the original).

    Books we build ourselves are simple and don't need kepubify's "intentionally left blank" title page.
    """
    outdir = Path(tempfile.mkdtemp(prefix="kepub-", dir=workdir))
    cmd = [config.KEPUBIFY, "-o", str(outdir)] + (["--no-add-dummy-titlepage"] if generated else []) + [str(src)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return None
    results = list(outdir.glob("*.epub"))
    return results[0] if results else None


def epub_title(path):
    """'Title - Author' from the EPUB's own metadata, or None. Download names like pg1342.epub say nothing."""
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "dc": "http://purl.org/dc/elements/1.1/"}
    try:
        with zipfile.ZipFile(path) as z:
            container = etree.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(".//c:rootfile", ns).get("full-path")
            opf = etree.fromstring(z.read(opf_path))
    except (KeyError, AttributeError, OSError, zipfile.BadZipFile, etree.XMLSyntaxError):
        return None
    title = opf.findtext(".//dc:title", namespaces=ns)
    author = opf.findtext(".//dc:creator", namespaces=ns)
    if not title or not title.strip():
        return None
    title = " ".join(title.split())
    return f"{title} - {' '.join(author.split())}" if author and author.strip() else title


def process_file(src, original_name, workdir, outdir=None, generated=False):
    suffix = suffix_of(original_name)
    if suffix not in SUPPORTED:
        raise ConvertError(f"{suffix or 'This file type'} isn't supported. Send EPUB, PDF, CBZ, MOBI, FB2, DJVU or TXT.")
    stem = safe_filename(stem_of(original_name))
    if suffix == ".epub":
        stem = safe_filename(epub_title(src) or stem)
        kepub = kepubify(src, workdir, generated)
        if kepub:
            return stem, publish(kepub, stem, ".kepub.epub", outdir)
    return stem, publish(src, stem, suffix, outdir)


def filename_from(response, url):
    disposition = response.headers.get("content-disposition")
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        name = msg.get_filename()
        if name:
            return PurePosixPath(name).name
    return PurePosixPath(unquote(urlsplit(url).path)).name or None


PAGE_ACCEPT = "text/html,application/xhtml+xml,application/epub+zip,application/pdf,*/*;q=0.8"


def fetch(url, accept=PAGE_ACCEPT):
    """GET a public link; if the site refuses our honest client name, try once more as a browser."""
    for i, agent in enumerate(config.USER_AGENTS):
        try:
            return fetch_as(url, agent, accept=accept)
        except Refused:
            if i == len(config.USER_AGENTS) - 1:
                raise ConvertError("The site refused the download") from None


class Refused(ConvertError):
    pass


def fetch_as(url, agent, accept=PAGE_ACCEPT):
    """GET a public link, checking every redirect hop and capping the size."""
    url = safe_url(url)
    headers = {"User-Agent": agent, "Accept": accept}
    with httpx.Client(follow_redirects=False, timeout=config.FETCH_TIMEOUT, headers=headers) as client:
        for _ in range(8):
            public_host(urlsplit(url).hostname)
            try:
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = safe_url(str(response.url.join(response.headers.get("location", ""))))
                        continue
                    if response.status_code in (401, 403, 429):
                        raise Refused(f"The site answered {response.status_code}")
                    if response.status_code >= 400:
                        raise ConvertError(f"The site answered {response.status_code}")
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > config.MAX_FETCH_BYTES:
                        raise ConvertError("That download is too big (over 50 MB)")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > config.MAX_FETCH_BYTES:
                            raise ConvertError("That download is too big (over 50 MB)")
                    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    return url, ctype, bytes(data), filename_from(response, url)
            except httpx.HTTPError as exc:
                raise ConvertError(f"Couldn't download the link: {exc.__class__.__name__}") from exc
    raise ConvertError("Too many redirects")


def clean_xhtml(fragment, keep=("href",), on_img=None):
    """Any HTML fragment as well-formed XHTML with presentational attributes removed."""
    if re.search(r"<body[\s>]", fragment, re.I):
        container = lhtml.document_fromstring(fragment).find("body")
    else:
        container = lhtml.fragment_fromstring(fragment or "", create_parent="div")
    for el in list(container.iter()):
        if not isinstance(el.tag, str):
            el.drop_tree()  # comments, processing instructions
            continue
        if el.tag in ("script", "style", "iframe", "object", "embed", "form", "head"):
            el.drop_tree()
            continue
        if el.tag == "img":
            # Remote pictures only survive if on_img brings them into the book; otherwise they're dropped.
            src, alt = el.get("src"), (el.get("alt") or "").strip()
            local = on_img(src, alt) if on_img else None
            if not local:
                el.drop_tree()
                continue
            el.attrib.clear()
            el.set("src", local)
            el.set("alt", alt[:300])
            continue
        for name in list(el.attrib):
            if name not in keep or not el.attrib[name].startswith(("http://", "https://")):
                del el.attrib[name]
    parts = [html.escape(container.text)] if container.text and container.text.strip() else []
    parts += [etree.tostring(child, method="xml", encoding="unicode") for child in container]
    return "".join(parts)


def shrink_image(data):
    """Grayscale JPEG sized for the e-ink screen, or None if it isn't a picture worth keeping."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()  # first frame of animations
    except Exception:
        return None
    w, h = im.size
    if min(w, h) < MIN_IMAGE_SIDE or max(w, h) > MAX_IMAGE_RATIO * min(w, h):
        return None  # icons, avatars, tracking pixels; banners and dividers
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        background = Image.new("RGBA", im.size, "white")
        background.alpha_composite(im)
        im = background
    im = im.convert("L")
    im.thumbnail(SCREEN_SIZE)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80, optimize=True)
    return out.getvalue()


def larger_version(url):
    """Wikipedia pages link 250px thumbnails; a 960px one is much sharper on the e-reader."""
    match = WIKIMEDIA_THUMB.match(url.split("?")[0])
    if match and int(match.group(2)) < 960:
        return f"{match.group(1)}960px-{match.group(3)}"
    return None


def strip_non_article(data):
    """The page without blocks that point to other content (see NOT_ARTICLE), ready for the extractor."""
    try:
        doc = lhtml.document_fromstring(data)
    except (etree.ParserError, ValueError):
        return data
    for el in list(doc.iter()):
        if not isinstance(el.tag, str) or el.tag in ("html", "body", "article", "main"):
            continue
        marker = f"{el.get('class', '')} {el.get('id', '')}"
        if (marker.strip() and NOT_ARTICLE.search(marker) and el.getparent() is not None
                and len(el.text_content().split()) < NOT_ARTICLE_MAX_WORDS):
            el.drop_tree()
    return etree.tostring(doc, encoding="utf-8", method="html")


class ArticleImages:
    """Collects the article's own pictures into the book: called once per <img> in the extracted body."""

    def __init__(self, base_url):
        self.base_url = base_url
        self.items = []   # (file name inside the EPUB, JPEG bytes)
        self.seen = {}    # url -> file name, or None if skipped
        self.started = time.monotonic()

    def __call__(self, src, alt):
        if not src or src.startswith("data:"):
            return None
        url = urljoin(self.base_url, src)
        if url in self.seen:
            return self.seen[url]
        self.seen[url] = None
        name = unquote(PurePosixPath(urlsplit(url).path).name)
        if NOT_CONTENT.search(name) or NOT_CONTENT.search(alt) or name.lower().endswith(".svg"):
            return None
        if len(self.items) >= MAX_IMAGES or time.monotonic() - self.started > IMAGE_BUDGET_SECONDS:
            return None
        data = None
        for candidate in (larger_version(url), url):
            if not candidate:
                continue
            try:
                data = fetch(candidate, accept=IMAGE_ACCEPT)[2]
                break
            except (ConvertError, ValueError) as exc:
                log.info("Skipped an article image: %s", exc)
        jpeg = shrink_image(data) if data else None
        if not jpeg:
            return None
        local = f"images/img{len(self.items) + 1:02d}.jpg"
        self.items.append((local, jpeg))
        self.seen[url] = local
        return local


def article_body(data, url, images=None):
    """The readable part of a web page, as well-formed XHTML; pictures go through `images`."""
    def extract(page):
        return trafilatura.extract(trafilatura.load_html(page), url=url, output_format="html",
                                   include_links=False, include_images=images is not None, include_tables=True,
                                   include_comments=False, favor_recall=True)

    # If a site wraps the whole article in a block that looks like a promo, fall back to the full page.
    body = extract(strip_non_article(data)) or extract(data)
    if not body:
        raise ConvertError("Couldn't find an article on that page")
    return clean_xhtml(body, keep=(), on_img=images)


def article_to_epub(data, url, title_hint, workdir):
    meta = trafilatura.extract_metadata(trafilatura.load_html(data), default_url=url)
    title = (getattr(meta, "title", None) or title_hint or urlsplit(url).hostname or "Article").strip()
    author = getattr(meta, "author", None)
    site = getattr(meta, "sitename", None) or urlsplit(url).hostname
    date = getattr(meta, "date", None)
    lang = (getattr(meta, "language", None) or "en")[:2]
    byline = " · ".join(html.escape(x) for x in (author, site, date) if x)
    header = f"<p class='meta'>{byline}</p><p class='meta'>{html.escape(url)}</p><hr/>"
    images = ArticleImages(url)
    body = article_body(data, url, images)
    out = make_epub(title, author, lang, header + body, workdir, images.items)
    return process_file(out, f"{safe_filename(title, 'Article')}.epub", workdir, generated=True)


def make_epub(title, author, lang, body, workdir, images=()):
    """A one-chapter EPUB around a well-formed XHTML body (plus its JPEGs); returns its path in workdir."""
    chapter = epub.EpubHtml(title=title, file_name="text.xhtml", lang=lang)
    chapter.content = (f"<html><head><title>{html.escape(title)}</title></head><body>"
                       f"<h1>{html.escape(title)}</h1>{body}</body></html>")
    css = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=ARTICLE_CSS)
    chapter.add_item(css)

    book = epub.EpubBook()
    book.set_identifier(f"kobo-bridge-{uuid.uuid4()}")
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    book.add_item(css)
    book.add_item(chapter)
    for i, (file_name, data) in enumerate(images, 1):
        book.add_item(epub.EpubImage(uid=f"img{i}", file_name=file_name, media_type="image/jpeg", content=data))
    book.toc = [chapter]
    book.spine = [chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    out = Path(tempfile.mkdtemp(prefix="epub-", dir=workdir)) / "book.epub"
    epub.write_epub(str(out), book)
    return out


def process_url(url, title_hint, workdir, outdir=None):
    final, ctype, data, name = fetch(url)
    suffix = CONTENT_TYPES.get(ctype)
    if not suffix and name and suffix_of(name) in SUPPORTED - {".htm", ".html"} and "html" not in ctype:
        suffix = suffix_of(name)
    if suffix:
        name = name if name and suffix_of(name) == suffix else f"{safe_filename(title_hint, 'download')}{suffix}"
        src = workdir / f"download{suffix}"
        src.write_bytes(data)
        return process_file(src, name, workdir, outdir)
    if not ctype or "html" in ctype or ctype.startswith("text/"):
        return article_to_epub(data, final, title_hint, workdir)
    raise ConvertError(f"Don't know what to do with {ctype} links")


def run(job, upload_dir):
    """Process one job; returns (title, output file name). Raises ConvertError for user-facing problems."""
    work_root = config.DATA_DIR / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root) as tmp:
        workdir = Path(tmp)
        if job["kind"] == "url":
            return process_url(job["source"], job["title"], workdir)
        if job["kind"] == "book":
            from . import books  # books builds on this module
            return books.fetch_book(job["source"], job["title"], workdir, config.BOOKS_OUTPUT_DIR)
        files = list((upload_dir / job["id"]).glob("*"))
        if not files:
            raise ConvertError("The uploaded file is missing")
        return process_file(files[0], job["source"], workdir)
