import ipaddress
import re
import socket
import unicodedata
from urllib.parse import urlsplit

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.I)


def safe_url(value):
    """An HTTP(S) URL without credentials or control characters."""
    value = (value or "").strip()
    url = urlsplit(value)
    if not value or len(value) > 2048 or url.scheme not in {"http", "https"} or not url.hostname \
            or url.username or url.password or any(ord(c) < 32 for c in value):
        raise ValueError("Use an HTTP(S) link without credentials")
    _ = url.port  # rejects malformed ports
    return value


def find_url(*texts):
    """Android shares often put the link inside the text field instead of the url field."""
    for text in texts:
        if not text:
            continue
        match = URL_IN_TEXT.search(text)
        if match:
            return match.group(0).rstrip(".,;:!?)]}")
    return None


def public_host(hostname):
    """Refuse links that resolve to this Pi, the LAN or the tailnet, so a shared link can't probe them."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Can't resolve {hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("Links to private or local addresses are not allowed")


def safe_filename(name, fallback="book"):
    name = unicodedata.normalize("NFKC", name or "")
    name = name.replace("/", " ").replace("\\", " ")
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or fallback)[:120]
