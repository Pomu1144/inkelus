"""Small HTTP helpers on urllib (standard library only).

Local and private-network hosts skip any corporate HTTP(S)_PROXY setting, so
talking to Ollama / LM Studio on 127.0.0.1 works on managed
work laptops too.
"""
import ipaddress
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_default = urllib.request.build_opener()


class HTTPError(Exception):
    def __init__(self, status, body):
        msg = body
        try:
            j = json.loads(body)
            err = j.get("error")
            msg = (err.get("message") if isinstance(err, dict) else err) or j.get("message") or body
        except Exception:
            pass
        super().__init__("HTTP %s%s" % (status, ": " + str(msg)[:300] if msg else ""))
        self.status = status


def is_local(host):
    h = (host or "").strip("[]").lower()
    if h in ("localhost", "host.docker.internal") or h.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def open_url(url, method="GET", body=None, headers=None, timeout=30):
    """Open a request and return the live response (caller closes it)."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    opener = _direct if is_local(urlsplit(url).hostname) else _default
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, e.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as e:
        raise ConnectionError("Can't reach %s (%s)" % (urlsplit(url).netloc, e.reason)) from None
    except OSError as e:
        raise ConnectionError("Can't reach %s (%s)" % (urlsplit(url).netloc, e)) from None


def get_json(url, method="GET", body=None, headers=None, timeout=30):
    with open_url(url, method, body, headers, timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def iter_lines(resp, cancel=None):
    """Yield decoded, non-empty lines from a streaming response."""
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        line = resp.readline()
        if not line:
            return
        line = line.decode("utf-8", "replace").strip()
        if line:
            yield line


class Cancelled(Exception):
    pass
