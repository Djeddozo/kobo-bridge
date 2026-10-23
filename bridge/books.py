"""Find free, legal ebooks on Standard Ebooks and Project Gutenberg.

Like a reading app's catalog: every search and every download is one the person asked for on the Kobo.
Claude itself never downloads books (Standard Ebooks asks AI agents not to); it can only suggest titles.
"""
import re
from urllib.parse import quote_plus, urljoin, urlsplit

from lxml import etree
from lxml import html as lhtml

from . import convert
from .security import safe_filename

STANDARD = "standardebooks.org"
GUTENBERG = "www.gutenberg.org"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
GUTENBERG_BOOK = re.compile(r"^https://(?:www\.)?gutenberg\.org/ebooks/(\d+)(?:\.opds)?/?$")
STANDARD_BOOK = re.compile(r"^https://standardebooks\.org/ebooks/[a-z0-9-]+/[a-z0-9-]+(?:/[a-z0-9-]+)*$")
DOWNLOAD_COUNT = re.compile(r"^\d[\d,.]*\s+downloads?$", re.I)
PER_SOURCE = 12


def search(query):
    """Standard Ebooks first (carefully formatted), then Gutenberg; a source that fails is just skipped."""
    query = " ".join((query or "").split())[:120]
    if not query:
        return []
    results = []
    for finder in (search_standard, search_gutenberg):
        try:
            results += finder(query)
        except (convert.ConvertError, ValueError, etree.XMLSyntaxError, etree.ParserError):
            continue
    return results


def search_standard(query):
    return standard_list(f"query={quote_plus(query)}", PER_SOURCE)


def search_gutenberg(query):
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3]
    return gutenberg_list(f"query={quote_plus(query)}", PER_SOURCE, words)


def standard_list(params, limit):
    """Books from a Standard Ebooks listing page (search, subject, newest, popular), with their covers."""
    _final, _ctype, data, _name = convert.fetch(f"https://{STANDARD}/ebooks?{params}")
    doc = lhtml.document_fromstring(data)
    results = []
    for li in doc.xpath('//li[@typeof="schema:Book"]')[:limit]:
        path = li.get("about") or ""
        names = [n.strip() for n in li.xpath('.//*[@property="schema:name"]/text()') if n.strip()]
        if not path.startswith("/ebooks/") or not names:
            continue
        cover = next((src for src in li.xpath(".//img/@src") if src.endswith(".jpg")), None)
        results.append({"source": "Standard Ebooks", "title": names[0],
                        "author": ", ".join(dict.fromkeys(names[1:])) or None,
                        "url": f"https://{STANDARD}{path}",
                        "cover": urljoin(f"https://{STANDARD}/", cover) if cover else None})
    return results


def gutenberg_list(params, limit, words=()):
    """Books from a Gutenberg OPDS feed (search, most downloaded), with their covers."""
    _final, _ctype, data, _name = convert.fetch(f"https://{GUTENBERG}/ebooks/search.opds/?{params}",
                                                accept="application/atom+xml,*/*;q=0.5")
    feed = convert.parse_xml(data)
    results = []
    for entry in feed.findall("a:entry", ATOM):
        match = GUTENBERG_BOOK.match((entry.findtext("a:id", namespaces=ATOM) or "").strip())
        if not match:
            continue  # "Authors", "Subjects" and similar navigation entries
        title = " ".join((entry.findtext("a:title", namespaces=ATOM) or "?").split())
        author = (entry.findtext("a:content", namespaces=ATOM) or "").strip()
        if DOWNLOAD_COUNT.match(author):
            author = ""  # some entries carry "161 downloads" where the author would be
        if words and not any(w in f"{title} {author}".lower() for w in words):
            continue  # the feed sometimes adds unrelated popular books
        number = match.group(1)
        results.append({"source": "Project Gutenberg", "title": title, "author": author or None,
                        "url": f"https://{GUTENBERG}/ebooks/{number}",
                        "cover": f"https://{GUTENBERG}/cache/epub/{number}/pg{number}.cover.medium.jpg"})
        if len(results) >= limit:
            break
    return results


def is_book_page(url):
    return bool(GUTENBERG_BOOK.match(url or "") or STANDARD_BOOK.match(url or ""))


def download_url(page_url):
    """The EPUB behind a book page from search()."""
    match = GUTENBERG_BOOK.match(page_url)
    if match:
        return f"https://{GUTENBERG}/ebooks/{match.group(1)}.epub3.images"
    if STANDARD_BOOK.match(page_url):
        _final, _ctype, data, _name = convert.fetch(page_url)
        links = lhtml.document_fromstring(data).xpath('//a/@href')
        epubs = [l for l in links if "/downloads/" in l and l.endswith(".epub")
                 and not l.endswith(".kepub.epub") and "_advanced" not in l]
        if not epubs:
            raise convert.ConvertError("Standard Ebooks has no download for this book here")
        # Without ?source=download the link answers with a "Your download has started" page.
        return urljoin(page_url, epubs[0]) + "?source=download"
    raise convert.ConvertError("Only Standard Ebooks and Project Gutenberg books can be fetched")


def fetch_book(page_url, title, workdir, outdir):
    """Download one book the person picked and put it in the library (as KEPUB)."""
    if not is_book_page(page_url):
        raise convert.ConvertError("Only Standard Ebooks and Project Gutenberg books can be fetched")
    url = download_url(page_url)
    if urlsplit(url).hostname not in (STANDARD, GUTENBERG, "gutenberg.org"):
        raise convert.ConvertError("Unexpected download location")
    _final, _ctype, data, name = convert.fetch(url, accept="application/epub+zip,*/*;q=0.5")
    if not data.startswith(b"PK"):  # EPUBs are ZIP files; anything else is an error page, never a "book"
        raise convert.ConvertError("The site didn't send an ebook file. Try again later.")
    src = workdir / "book.epub"
    src.write_bytes(data)
    name = name if name and name.lower().endswith(".epub") else f"{safe_filename(title, 'Book')}.epub"
    return convert.process_file(src, name, workdir, outdir)
