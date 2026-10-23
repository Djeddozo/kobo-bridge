#!/bin/sh
# Installs or updates Kobo Bridge from this source folder. Run with: sudo sh deploy/install.sh
# Touches only /opt/kobo-bridge, /var/lib/kobo-bridge, its own unit and its own Tailscale Serve port.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/kobo-bridge
DATA=/var/lib/kobo-bridge
PORT=8850
KOBO_PORT=8851
USER_NAME=djeddozo

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo" >&2; exit 1; }

# Refuse to take over a port someone else already serves.
if tailscale serve status 2>/dev/null | grep -q ":$PORT" && ! tailscale serve status | grep -A1 ":$PORT" | grep -q "127.0.0.1:$PORT"; then
    echo "Tailscale port $PORT is already used by something else" >&2; exit 1
fi
if tailscale serve status 2>/dev/null | grep -q ":$KOBO_PORT" && ! tailscale serve status | grep -A1 ":$KOBO_PORT" | grep -q "127.0.0.1:$PORT"; then
    echo "Tailscale port $KOBO_PORT is already used by something else" >&2; exit 1
fi
if tailscale funnel status 2>/dev/null | grep -qE "Funnel on.*:($PORT|$KOBO_PORT)"; then
    echo "Funnel is on for port $PORT or $KOBO_PORT; refusing" >&2; exit 1
fi

mkdir -p "$APP"
rsync -a --delete --exclude .venv --exclude .pytest_cache --exclude __pycache__ --exclude tests "$SRC/" "$APP/"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --disable-pip-version-check --require-virtualenv -r "$APP/requirements.lock"
chown -R root:root "$APP"
chmod -R go-w "$APP"

install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "$DATA"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0755 "/home/$USER_NAME/kobo/library"
# Discover's article feeds: created once, then it's the owner's file to edit.
SOURCES="/home/$USER_NAME/kobo/sources.json"
if [ ! -f "$SOURCES" ]; then
    cat > "$SOURCES" <<'EOF'
{
  "articles": [
    {"name": "The Micro Philosopher", "feed": "https://themicrophilosopher.com/feed"},
    {"name": "Aeon", "feed": "https://aeon.co/feed.rss"}
  ]
}
EOF
    chown "$USER_NAME:$USER_NAME" "$SOURCES"
fi

install -m 0644 "$SRC/deploy/kobo-bridge.service" /etc/systemd/system/kobo-bridge.service
systemctl daemon-reload
systemctl enable kobo-bridge.service >/dev/null
systemctl restart kobo-bridge.service

tailscale serve --bg --https="$PORT" "http://127.0.0.1:$PORT" >/dev/null
# Plain HTTP for the Kobo plugin (KOReader can't do HTTPS through Tailscale's userspace proxy).
tailscale serve --bg --http="$KOBO_PORT" "http://127.0.0.1:$PORT" >/dev/null
echo "Kobo Bridge is running: $(systemctl is-active kobo-bridge)"
tailscale serve status | grep -A1 -E ":($PORT|$KOBO_PORT)"
