#!/bin/sh
# End-to-end check against real sites, on a temporary port and a throwaway library (never the real one).
set -u
cd "$(dirname "$0")/.."
T=$(mktemp -d)
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null; rm -rf "$T"' EXIT
KOBO_BRIDGE_PORT=8851 KOBO_BRIDGE_DATA="$T/data" KOBO_BRIDGE_LIBRARY="$T/library" .venv/bin/python -m bridge >"$T/log" 2>&1 &
PID=$!
for i in $(seq 1 20); do curl -s -o /dev/null http://127.0.0.1:8851/ && break; sleep 0.5; done

post() { curl -s -H "X-Kobo-Bridge: 1" -H "Content-Type: application/json" -d "{\"url\":\"$1\"}" http://127.0.0.1:8851/api/links; echo; }
post "https://docs.python.org/3/whatsnew/3.13.html"
[ -n "${KOBO_BRIDGE_CONTACT:-}" ] && post "https://en.wikipedia.org/wiki/E_Ink"
post "https://www.gutenberg.org/ebooks/1342.epub3.images"
curl -s -o /dev/null -w "share: %{http_code} -> %{redirect_url}\n" -F "title=Share test" \
    -F "text=check https://www.gutenberg.org/ebooks/11.epub.noimages" http://127.0.0.1:8851/share

for i in $(seq 1 60); do
    curl -s http://127.0.0.1:8851/api/jobs | grep -qE '"(queued|running)"' || break
    sleep 2
done
curl -s http://127.0.0.1:8851/api/jobs | .venv/bin/python -c 'import json,sys; [print(j["status"], "|", j["output"], "|", j["error"]) for j in json.load(sys.stdin)["jobs"]]'
echo "--- files"
ls -la "$T/library/Kobo Bridge"
echo "--- server errors"
grep -iE "error|traceback" "$T/log" || echo none
