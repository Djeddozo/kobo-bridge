import hmac
import logging
import queue
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import books, claude, config, convert, discover
from .jobs import JobStore
from .security import find_url, safe_filename, safe_url

log = logging.getLogger("kobo-bridge")
STATIC = Path(__file__).parent / "static"


class Worker:
    """Converts one job at a time, so a big PDF can't starve the Pi's other services."""

    def __init__(self, store, upload_dir):
        self.store, self.upload_dir = store, upload_dir
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self.loop, name="converter", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.queue.put(None)
        self.thread.join(timeout=10)

    def submit(self, job_id):
        self.queue.put(job_id)

    def loop(self):
        while (job_id := self.queue.get()) is not None:
            job = self.store.get(job_id)
            if not job or job["status"] != "queued":
                continue
            self.store.update(job_id, status="running")
            try:
                title, output = convert.run(job, self.upload_dir)
                self.store.update(job_id, status="done", title=title, output=output, error=None)
            except (convert.ConvertError, ValueError) as exc:
                self.store.update(job_id, status="failed", error=str(exc))
            except Exception:
                log.exception("Job %s crashed", job_id)
                self.store.update(job_id, status="failed", error="Something went wrong while converting this")
            finally:
                shutil.rmtree(self.upload_dir / job_id, ignore_errors=True)


class Link(BaseModel):
    url: str
    title: Optional[str] = None


class BookPick(BaseModel):
    url: str
    title: Optional[str] = None


class Ask(BaseModel):
    prompt: str
    context: Optional[str] = None
    book: Optional[str] = None
    conversation: Optional[str] = None


def create_app():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    upload_dir = config.DATA_DIR / "uploads"
    upload_dir.mkdir(exist_ok=True)
    store = JobStore(config.DATA_DIR / "jobs.sqlite3")
    worker = Worker(store, upload_dir)
    asks = claude.AskStore(config.DATA_DIR / "asks.sqlite3")
    claude_worker = claude.ClaudeWorker(asks)
    token = claude.load_token()
    shelves = discover.Discover()

    @asynccontextmanager
    async def lifespan(_app):
        worker.start()
        claude_worker.start()
        for job_id in store.recover():
            worker.submit(job_id)
        asks.recover()
        yield
        worker.stop()
        claude_worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    port = config.PORT
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", urlsplit(config.ORIGIN).netloc} | config.EXTRA_HOSTS
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", config.ORIGIN}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Unknown host"}, status_code=421)
        if request.url.path.startswith(("/api/claude", "/api/books", "/api/discover")):
            # Claude runs on the owner's account: only the Kobo plugin, which holds the token, may use it.
            sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(sent.encode(), token.encode()):
                return JSONResponse({"detail": "Missing or wrong token"}, status_code=401)
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            if request.url.path == "/share":
                # Android's share sheet posts here as a plain navigation without our header; other sites are still refused.
                refused = cross_site or (origin not in (None, "null") and origin not in allowed_origins)
            else:
                refused = (request.headers.get("x-kobo-bridge") != "1" or cross_site
                           or request.headers.get("origin", config.ORIGIN) not in allowed_origins)
            if refused:
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    async def save_upload(file: UploadFile):
        name = safe_filename(Path(file.filename or "").name, "upload")
        if convert.suffix_of(name) not in convert.SUPPORTED:
            raise HTTPException(400, f"{name}: send EPUB, PDF, CBZ, MOBI, FB2, DJVU or TXT")
        job_id = store.add("file", name)
        target_dir = upload_dir / job_id
        target_dir.mkdir()
        size = 0
        with open(target_dir / name, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    out.close()
                    shutil.rmtree(target_dir, ignore_errors=True)
                    store.update(job_id, status="failed", error="File is too big (over 200 MB)")
                    return job_id
                out.write(chunk)
        worker.submit(job_id)
        return job_id

    def add_link(url, title=None):
        job_id = store.add("url", safe_url(url), (title or "").strip()[:200] or None)
        worker.submit(job_id)
        return job_id

    @app.post("/share")
    async def share(title: str = Form(""), text: str = Form(""), url: str = Form(""),
                    file: list[UploadFile] = File(default=[])):
        count = 0
        for upload in file:
            if upload.filename:
                try:
                    await save_upload(upload)
                    count += 1
                except HTTPException:
                    pass
        if not count:
            link = find_url(url, text, title)
            if link:
                try:
                    add_link(link, title if title and title != link else None)
                    count = 1
                except ValueError:
                    pass
        return RedirectResponse(f"/?shared={count}", status_code=303)

    @app.post("/api/links")
    async def post_link(link: Link):
        try:
            return {"id": add_link(link.url, link.title)}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/files")
    async def post_files(file: list[UploadFile] = File(...)):
        return {"ids": [await save_upload(f) for f in file]}

    @app.post("/api/claude")
    async def ask_claude(ask: Ask):
        prompt = ask.prompt.strip()
        if not prompt:
            raise HTTPException(400, "Type a question first")
        conversation = ask.conversation if ask.conversation and asks.get(ask.conversation) else None
        if conversation:
            conversation = asks.get(conversation)["conversation"]
        ask_id = asks.add(prompt[:4000], (ask.context or "").strip()[:8000] or None,
                          (ask.book or "").strip()[:300] or None, conversation)
        claude_worker.submit(ask_id)
        return {"id": ask_id, "conversation": asks.get(ask_id)["conversation"]}

    @app.get("/api/claude/{ask_id}")
    async def get_ask(ask_id: str):
        item = asks.get(ask_id)
        if not item:
            raise HTTPException(404, "Unknown question")
        return {**item, "css": claude.ANSWER_CSS}

    @app.get("/api/claude")
    async def recent_asks():
        return {"asks": [{k: a[k] for k in ("id", "conversation", "created", "status", "prompt", "book", "files")}
                         for a in asks.recent()]}

    @app.get("/api/books/search")
    def search_books(q: str = ""):
        # Plain def: FastAPI runs it in a thread, so slow catalogs don't block other requests.
        return {"results": books.search(q)}

    @app.post("/api/books/get")
    async def get_book(pick: BookPick):
        if not books.is_book_page(pick.url):
            raise HTTPException(400, "Only Standard Ebooks and Project Gutenberg books can be fetched")
        job_id = store.add("book", pick.url, (pick.title or "").strip()[:200] or None)
        worker.submit(job_id)
        return {"id": job_id, "folder": config.BOOKS_OUTPUT_DIR.name}

    @app.get("/api/books/job/{job_id}")
    async def book_job(job_id: str):
        job = store.get(job_id)
        if not job or job["kind"] not in ("book", "url"):
            raise HTTPException(404, "Unknown download")
        return job

    @app.get("/api/discover")
    async def discover_tabs():
        return shelves.tabs()

    @app.get("/api/discover/shelf/{name}")
    def discover_shelf(name: str, page: int = 1):
        # Plain def: FastAPI runs it in a thread, so a slow catalog doesn't block other requests.
        try:
            return shelves.shelf(name, page)
        except KeyError:
            raise HTTPException(404, "Unknown shelf")
        except convert.ConvertError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/discover/cover/{cid}")
    def discover_cover(cid: str):
        path = shelves.cover_path(cid)
        if not path:
            raise HTTPException(404, "No cover")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})

    @app.post("/api/discover/article")
    async def discover_article(link: Link):
        try:
            job_id = add_link(link.url, link.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"id": job_id, "folder": config.OUTPUT_DIR.name}

    @app.get("/api/jobs")
    async def jobs():
        free = shutil.disk_usage(config.DATA_DIR).free
        return {"jobs": store.recent(), "free_bytes": free, "output_folder": str(config.OUTPUT_DIR)}

    def static_file(name, media_type=None, cache="no-cache"):
        return FileResponse(STATIC / name, media_type=media_type, headers={"Cache-Control": cache})

    @app.get("/")
    async def index():
        return static_file("index.html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return static_file("manifest.webmanifest", "application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return static_file("sw.js", "text/javascript")

    @app.get("/offline.html")
    async def offline():
        return static_file("offline.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
