"use strict";

const $ = (id) => document.getElementById(id);
const HEADERS = { "X-Kobo-Bridge": "1" };
const STATUS = { queued: "Waiting", running: "Converting", done: "On its way", failed: "Failed" };
let pollTimer;

function banner(text, kind) {
  const el = $("banner");
  el.textContent = text;
  el.className = `banner ${kind}`;
  el.hidden = false;
}

function ago(seconds) {
  const s = Math.max(0, Date.now() / 1000 - seconds);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}

function render(data) {
  const list = $("jobs");
  list.replaceChildren();
  for (const job of data.jobs) {
    const li = document.createElement("li");
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = job.output || job.title || job.source;
    const pill = document.createElement("span");
    pill.className = `pill ${job.status}`;
    pill.textContent = STATUS[job.status] || job.status;
    row.append(name, pill);
    const meta = document.createElement("div");
    meta.className = "meta";
    const origin = { url: job.source, book: "Free book (from the Kobo)" }[job.kind] || "Uploaded file";
    meta.textContent = `${origin} · ${ago(job.created)}`;
    li.append(row, meta);
    if (job.error) {
      const err = document.createElement("div");
      err.className = "error";
      err.textContent = job.error;
      li.append(err);
    }
    list.append(li);
  }
  $("empty").hidden = data.jobs.length > 0;
  const gb = (data.free_bytes / 1e9).toFixed(1);
  $("footer").textContent = `Books land in ${data.output_folder} and sync to your Kobo over Wi-Fi. ${gb} GB free on the Pi.`;
}

async function refresh() {
  clearTimeout(pollTimer);
  let busy = false;
  try {
    const res = await fetch("/api/jobs", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    render(data);
    busy = data.jobs.some((j) => j.status === "queued" || j.status === "running");
  } catch {
    $("footer").textContent = "Can't reach the Pi. Is Tailscale connected?";
  }
  pollTimer = setTimeout(refresh, busy ? 2000 : 15000);
}

async function send(url, options, button) {
  button.disabled = true;
  try {
    const res = await fetch(url, { method: "POST", ...options, headers: { ...HEADERS, ...(options.headers || {}) } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Error ${res.status}`);
    banner("Sent! It'll be on your Kobo the next time it syncs.", "ok");
    return true;
  } catch (err) {
    banner(String(err.message || err), "bad");
    return false;
  } finally {
    button.disabled = false;
    refresh();
  }
}

$("link-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("link");
  const ok = await send("/api/links", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: input.value.trim() }),
  }, event.submitter);
  if (ok) input.value = "";
});

$("files").addEventListener("change", () => {
  const files = $("files").files;
  $("file-label").textContent = files.length ? Array.from(files, (f) => f.name).join(", ") : "Choose files";
});

$("file-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const files = $("files").files;
  if (!files.length) return banner("Choose a file first.", "bad");
  const form = new FormData();
  for (const f of files) form.append("file", f);
  const ok = await send("/api/files", { body: form }, event.submitter);
  if (ok) {
    $("files").value = "";
    $("file-label").textContent = "Choose files";
  }
});

let installPrompt;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  $("install").hidden = false;
});
$("install").addEventListener("click", async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  $("install").hidden = true;
});

const shared = new URLSearchParams(location.search).get("shared");
if (shared !== null) {
  if (Number(shared) > 0) banner("Sent! It'll be on your Kobo the next time it syncs.", "ok");
  else banner("Nothing to send there. Share a web link or an ebook file.", "bad");
  history.replaceState(null, "", "/");
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
refresh();
