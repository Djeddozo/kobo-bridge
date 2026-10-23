import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from bridge import claude, config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "library" / "Kobo Bridge")
    monkeypatch.setattr(config, "CLAUDE_OUTPUT_DIR", tmp_path / "library" / "Claude")
    monkeypatch.setattr(config, "EXTRA_HOSTS", {"100.64.0.7:8851"})
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    """A fake Claude Code: records each run, and writes a document when asked for one."""
    seen = []

    def fake_run(prompt, workspace, session_id=None):
        seen.append({"prompt": prompt, "workspace": workspace, "session": session_id})
        if "study guide" in prompt:
            (workspace / "out" / "guide.md").write_text("# Moby-Dick study guide\n\n## Themes\n\n- **Obsession**\n- Fate\n")
            return {"result": "I made a **study guide** for you.", "session_id": "s1", "total_cost_usd": 0.1}
        return {"result": "Ishmael is the *narrator*.\n\n- one\n- two", "session_id": "s1" if not session_id else "s2"}

    monkeypatch.setattr(claude, "run_claude", fake_run)
    return seen


@pytest.fixture
def client(env, calls):
    from bridge.main import create_app
    with TestClient(create_app(), base_url="http://100.64.0.7:8851") as c:
        c.headers.update({"x-kobo-bridge": "1",
                          "authorization": f"Bearer {(config.DATA_DIR / 'kobo-token').read_text()}"})
        yield c


def wait(client, ask_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        item = client.get(f"/api/claude/{ask_id}").json()
        if item["status"] in {"done", "failed"}:
            return item
        time.sleep(0.1)
    raise AssertionError("ask did not finish")


def test_token_required(client):
    r = client.post("/api/claude", json={"prompt": "hi"}, headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = client.get("/api/claude", headers={"authorization": ""})
    assert r.status_code == 401
    assert (config.DATA_DIR / "kobo-token").stat().st_mode & 0o077 == 0


def test_ask_with_book_context_and_follow_up(client, calls):
    r = client.post("/api/claude", json={"prompt": "Who is speaking?", "context": "Call me Ishmael.",
                                         "book": "Moby-Dick - Herman Melville"})
    first = wait(client, r.json()["id"])
    assert first["status"] == "done"
    assert "<em>narrator</em>" in first["html"] and "<li>one</li>" in first["html"]
    assert 'reading "Moby-Dick - Herman Melville"' in calls[0]["prompt"]
    assert "Call me Ishmael." in calls[0]["prompt"]
    assert calls[0]["session"] is None

    r = client.post("/api/claude", json={"prompt": "And why?", "conversation": first["id"]})
    second = wait(client, r.json()["id"])
    assert second["conversation"] == first["conversation"]
    assert calls[1]["session"] == "s1"                   # follow-up resumes the same Claude session
    assert calls[1]["workspace"] == calls[0]["workspace"]  # in the same folder


def test_documents_become_ebooks(client):
    r = client.post("/api/claude", json={"prompt": "Make me a study guide for Moby-Dick"})
    item = wait(client, r.json()["id"])
    assert item["status"] == "done", item
    assert item["files"] == ["Moby-Dick study guide - Claude.kepub.epub"]
    assert "Sent to your library" in item["html"]
    book = config.CLAUDE_OUTPUT_DIR / item["files"][0]
    with zipfile.ZipFile(book) as z:
        text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith("html"))
    # kepubify wraps words in Kobo's <span class="koboSpan"> markers
    assert re.search(r"<strong>(<span[^>]*>)?Obsession", text) and "Themes" in text
    assert "intentionally left blank" not in text.lower()
    assert client.get("/api/claude").json()["asks"][0]["files"] == item["files"]


def test_claude_failure_is_reported(client, monkeypatch):
    def broken(prompt, workspace, session_id=None):
        raise RuntimeError("Claude Code: not logged in")
    monkeypatch.setattr(claude, "run_claude", broken)
    r = client.post("/api/claude", json={"prompt": "hi"})
    item = wait(client, r.json()["id"])
    assert item["status"] == "failed" and "not logged in" in item["error"]


def test_html_is_cleaned():
    out = claude.to_xhtml('Hi <script>alert(1)</script><a href="javascript:x" onclick="y">link</a> & more')
    assert "script" not in out and "onclick" not in out and "javascript" not in out and "&amp;" in out
