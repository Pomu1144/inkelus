"""The API tab: send HTTP requests, keep them in collections, check the responses, and run your dev server.

Requests go out from this app's local server, not the browser, so there are no CORS limits and
localhost / intranet APIs work. Collections, environments and history are saved in api.json in the
app's data folder. A collection belongs to a project or is global.

Environments hold {{variables}}. A variable marked secret is never shown to the AI: the AI writes
{{token}} and the value is filled in here, and it's masked in anything the AI reads back.
"""
import base64
import json
import os
import re
import shlex
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import net
from .store import new_id

MAX_BODY = 5 * 1024 * 1024
MAX_HISTORY = 150
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MASK = "••••••"


# ── storage ──────────────────────────────────────────────────────────────
def blank_request(name="New request", method="GET", url=""):
    return {"id": new_id(), "name": name, "method": method, "url": url, "params": [], "headers": [],
            "body": {"mode": "none", "text": "", "form": []}, "auth": {"type": "none"}, "checks": []}


class Store:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "api.json")
        self.lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        for k, v in (("collections", []), ("environments", []), ("active_env", {}), ("history", [])):
            if not isinstance(self.data.get(k), type(v)):
                self.data[k] = v

    def save(self):
        with self.lock:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)       # environments can hold tokens
            except OSError:
                pass

    def scope(self, project):
        """Collections and environments for a project ("" = global), plus the global ones."""
        p = project or ""
        cols = [c for c in self.data["collections"] if c.get("project", "") in (p, "")]
        envs = [e for e in self.data["environments"] if e.get("project", "") in (p, "")]
        hist = [h for h in self.data["history"] if h.get("project", "") == p][:MAX_HISTORY]
        return {"collections": cols, "environments": envs, "active_env": self.data["active_env"].get(p, ""),
                "history": hist}

    def collection(self, cid):
        return next((c for c in self.data["collections"] if c["id"] == cid), None)

    def find_request(self, rid):
        for c in self.data["collections"]:
            for r in c["requests"]:
                if r["id"] == rid:
                    return c, r
        return None, None

    def env(self, project):
        eid = self.data["active_env"].get(project or "", "")
        return next((e for e in self.data["environments"] if e["id"] == eid), None)

    def add_history(self, project, req, res, by):
        item = {"id": new_id(), "project": project or "", "when": time.time(), "by": by,
                "request": {k: req.get(k) for k in ("name", "method", "url", "params", "headers", "body", "auth")},
                "status": res.get("status"), "ms": res.get("ms"), "error": res.get("error", ""),
                "url": res.get("url", "")}
        with self.lock:
            self.data["history"].insert(0, item)
            del self.data["history"][MAX_HISTORY * 4:]
            self.save()
        return item


# ── variables ────────────────────────────────────────────────────────────
VAR = re.compile(r"\{\{\s*([\w.$-]+)\s*\}\}")


def variables(env):
    """{name: value} from an environment, and the set of secret values (to mask)."""
    vals, secrets = {}, set()
    for v in (env or {}).get("vars") or []:
        if v.get("enabled", True) and v.get("key"):
            vals[v["key"]] = str(v.get("value") or "")
            if v.get("secret") and v.get("value"):
                secrets.add(str(v["value"]))
    return vals, secrets


def fill(text, vals, missing=None):
    def sub(m):
        k = m.group(1)
        if k == "$timestamp":
            return str(int(time.time()))
        if k == "$isoTimestamp":
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if k == "$uuid" or k == "$guid":
            return str(uuid.uuid4())
        if k == "$randomInt":
            return str(int.from_bytes(os.urandom(2), "big") % 1000)
        if k in vals:
            return vals[k]
        if missing is not None:
            missing.add(k)
        return m.group(0)
    return VAR.sub(sub, text or "")


def mask(text, secrets):
    for s in sorted(secrets, key=len, reverse=True):
        if len(s) >= 4:
            text = text.replace(s, MASK)
    return text


# ── building and sending a request ───────────────────────────────────────
def build(req, vals):
    """The concrete request: method, URL (with params and API-key auth), headers and body bytes."""
    missing = set()
    method = (req.get("method") or "GET").upper()
    url = fill(req.get("url", ""), vals, missing).strip()
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    pairs = [(fill(p.get("key"), vals, missing), fill(p.get("value"), vals, missing))
             for p in req.get("params") or [] if p.get("enabled", True) and p.get("key")]
    headers = []
    for h in req.get("headers") or []:
        if h.get("enabled", True) and h.get("key"):
            headers.append((fill(h["key"], vals, missing).strip(), fill(h.get("value"), vals, missing)))
    auth = req.get("auth") or {}
    t = auth.get("type", "none")
    if t == "bearer" and auth.get("token"):
        headers.append(("Authorization", "Bearer " + fill(auth["token"], vals, missing)))
    elif t == "basic":
        raw = "%s:%s" % (fill(auth.get("username"), vals, missing), fill(auth.get("password"), vals, missing))
        headers.append(("Authorization", "Basic " + base64.b64encode(raw.encode()).decode()))
    elif t == "apikey" and auth.get("key"):
        k, v = fill(auth["key"], vals, missing), fill(auth.get("value"), vals, missing)
        if auth.get("in") == "query":
            pairs.append((k, v))
        else:
            headers.append((k, v))
    if pairs:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)
    body = req.get("body") or {}
    mode = body.get("mode", "none")
    data = None
    has = {k.lower() for k, _ in headers}
    if mode == "json":
        data = fill(body.get("text"), vals, missing).encode()
        if "content-type" not in has:
            headers.append(("Content-Type", "application/json"))
    elif mode == "text":
        data = fill(body.get("text"), vals, missing).encode()
        if "content-type" not in has:
            headers.append(("Content-Type", "text/plain; charset=utf-8"))
    elif mode == "form":
        data = urllib.parse.urlencode([(fill(f.get("key"), vals, missing), fill(f.get("value"), vals, missing))
                                       for f in body.get("form") or [] if f.get("enabled", True) and f.get("key")]).encode()
        if "content-type" not in has:
            headers.append(("Content-Type", "application/x-www-form-urlencoded"))
    if "user-agent" not in has:
        headers.append(("User-Agent", "VSCodeAssistant-API/1.0"))
    if "accept" not in has:
        headers.append(("Accept", "*/*"))
    return {"method": method, "url": url, "headers": headers, "data": data, "missing": sorted(missing)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def is_local(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost") or net.is_local(host)


def send(req, vals, timeout=30, follow=True, verify=True):
    """Send one request. Never raises for HTTP errors: a 404 or 500 is a normal result."""
    b = build(req, vals)
    out = {"method": b["method"], "url": b["url"], "missing": b["missing"], "sent_headers": b["headers"]}
    if not b["url"] or not urllib.parse.urlsplit(b["url"]).hostname:
        out["error"] = "Enter a URL, e.g. http://localhost:3000/api/users"
        return out
    handlers = []
    if is_local(b["url"]):
        handlers.append(urllib.request.ProxyHandler({}))   # skip corporate proxies for local addresses
    if not follow:
        handlers.append(_NoRedirect())
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    r = urllib.request.Request(b["url"], data=b["data"], method=b["method"])
    for k, v in b["headers"]:
        r.add_header(k, v)
    t0 = time.time()
    resp = None
    try:
        resp = opener.open(r, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = e
    except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        msg = str(reason)
        if isinstance(reason, ConnectionRefusedError) or "refused" in msg.lower():
            msg = "Connection refused: nothing is listening at %s. Is the server running?" % urllib.parse.urlsplit(b["url"]).netloc
        elif "timed out" in msg.lower():
            msg = "No answer within %d seconds." % timeout
        elif "certificate" in msg.lower():
            msg += " (turn off certificate checks for self-signed local servers)"
        out.update(error=msg, ms=int((time.time() - t0) * 1000))
        return out
    try:
        raw = resp.read(MAX_BODY + 1)
    except (OSError, socket.timeout) as e:
        raw = b""
        out["error"] = "The response was cut off: %s" % e
    ms = int((time.time() - t0) * 1000)
    truncated = len(raw) > MAX_BODY
    raw = raw[:MAX_BODY]
    hdrs = list(resp.headers.items()) if resp.headers else []
    ctype = next((v for k, v in hdrs if k.lower() == "content-type"), "")
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    binary = b"\0" in raw[:2048] or re.match(r"(image|audio|video|font)/|application/(octet-stream|pdf|zip)", ctype, re.I)
    out.update(status=getattr(resp, "status", None) or resp.getcode(), reason=getattr(resp, "reason", "") or "",
               headers=hdrs, size=len(raw), ms=ms, final_url=resp.geturl() if hasattr(resp, "geturl") else b["url"],
               content_type=ctype, truncated=truncated)
    if binary:
        out["body_b64"] = base64.b64encode(raw[:2 * 1024 * 1024]).decode()
        out["body"] = ""
    else:
        try:
            out["body"] = raw.decode(m.group(1) if m else "utf-8", "replace")
        except LookupError:
            out["body"] = raw.decode("utf-8", "replace")
        try:
            out["json"] = json.loads(out["body"]) if out["body"].strip()[:1] in ("{", "[") else None
        except ValueError:
            out["json"] = None
    return out


# ── checks on a response ─────────────────────────────────────────────────
def json_get(doc, path):
    """Value at a path like data.items[0].id (a leading $ or $. is allowed). Raises KeyError if absent."""
    p = (path or "").strip()
    p = re.sub(r"^\$\.?", "", p)
    cur = doc
    for name, idx in re.findall(r"([^.\[\]]+)|\[(-?\d+)\]", p):
        if idx:
            if not isinstance(cur, list):
                raise KeyError(path)
            cur = cur[int(idx)]
        else:
            if isinstance(cur, dict) and name in cur:
                cur = cur[name]
            elif isinstance(cur, list) and name.isdigit():
                cur = cur[int(name)]
            elif name == "length" and isinstance(cur, (list, str, dict)):
                cur = len(cur)
            else:
                raise KeyError(path)
    return cur


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jtype(v):
    return "null" if v is None else "boolean" if isinstance(v, bool) else "number" if isinstance(v, (int, float)) \
        else "string" if isinstance(v, str) else "array" if isinstance(v, list) else "object"


def run_checks(checks, res):
    """[{label, pass, actual}] for each check. Kinds: status, time, header, body, json."""
    out = []
    for c in checks or []:
        if not c.get("enabled", True):
            continue
        kind, op, path, want = c.get("kind", "status"), c.get("op", "eq"), c.get("path", ""), str(c.get("value", ""))
        label, ok, actual = "", False, ""
        if res.get("error"):
            out.append({"label": describe(c), "pass": False, "actual": res["error"]})
            continue
        try:
            if kind == "status":
                st = res.get("status")
                actual = str(st)
                if re.fullmatch(r"[1-5]xx", want.lower()):
                    ok = st is not None and str(st)[0] == want[0]
                elif op == "ne":
                    ok = str(st) != want
                else:
                    ok = str(st) in [w.strip() for w in want.split(",")]
            elif kind == "time":
                actual = "%s ms" % res.get("ms")
                ok = res.get("ms") is not None and _num(want) is not None and res["ms"] < _num(want)
            elif kind == "header":
                vals = [v for k, v in res.get("headers") or [] if k.lower() == path.lower()]
                actual = vals[0] if vals else "(missing)"
                ok = bool(vals) if op == "exists" else bool(vals) and (vals[0] == want if op == "eq" else want.lower() in vals[0].lower())
            elif kind == "body":
                body = res.get("body") or ""
                actual = (body[:80] + "…") if len(body) > 80 else body
                ok = (want in body) if op != "not_contains" else (want not in body)
            elif kind == "json":
                doc = res.get("json")
                if doc is None:
                    raise KeyError("not JSON")
                try:
                    v = json_get(doc, path)
                    found = True
                except (KeyError, IndexError, TypeError):
                    v, found = None, False
                actual = json.dumps(v, ensure_ascii=False)[:120] if found else "(missing)"
                if op == "exists":
                    ok = found
                elif op == "missing":
                    ok = not found
                elif not found:
                    ok = False
                elif op == "eq":
                    ok = _equal(v, want)
                elif op == "ne":
                    ok = not _equal(v, want)
                elif op == "contains":
                    ok = (want in v) if isinstance(v, str) else any(_equal(x, want) for x in v) if isinstance(v, list) else False
                elif op == "type":
                    ok = _jtype(v) == want.strip().lower()
                elif op in ("gt", "lt", "gte", "lte"):
                    a, w = _num(len(v) if isinstance(v, (list, str)) else v), _num(want)
                    ok = a is not None and w is not None and {"gt": a > w, "lt": a < w, "gte": a >= w, "lte": a <= w}[op]
                elif op == "length":
                    ok = isinstance(v, (list, str, dict)) and _num(want) == len(v)
        except KeyError as e:
            actual = "response isn't JSON" if str(e) == "'not JSON'" else "(missing)"
            ok = False
        out.append({"label": label or describe(c), "pass": bool(ok), "actual": actual})
    return out


def _equal(v, want):
    if isinstance(v, bool):
        return want.strip().lower() == str(v).lower()
    if isinstance(v, (int, float)) and _num(want) is not None:
        return float(v) == _num(want)
    if v is None:
        return want.strip().lower() in ("null", "none", "")
    if isinstance(v, (dict, list)):
        try:
            return v == json.loads(want)
        except ValueError:
            return False
    return str(v) == want


OP_WORDS = {"eq": "is", "ne": "is not", "exists": "exists", "missing": "is absent", "contains": "contains",
            "not_contains": "doesn't contain", "type": "is a", "gt": ">", "lt": "<", "gte": "≥", "lte": "≤",
            "length": "has length"}


def describe(c):
    kind, op, path, want = c.get("kind"), c.get("op", "eq"), c.get("path", ""), c.get("value", "")
    if kind == "status":
        return "Status %s %s" % ("is not" if op == "ne" else "is", want)
    if kind == "time":
        return "Responds in under %s ms" % want
    if kind == "header":
        return "Header %s %s" % (path, "exists" if op == "exists" else "%s %s" % (OP_WORDS.get(op, op), want))
    if kind == "body":
        return "Body %s %r" % (OP_WORDS.get(op, op), want)
    w = "" if op in ("exists", "missing") else " " + str(want)
    return "JSON %s %s%s" % (path or "$", OP_WORDS.get(op, op), w)


def default_checks():
    return [{"kind": "status", "op": "eq", "value": "2xx", "enabled": True}]


# ── curl and Postman ─────────────────────────────────────────────────────
def from_curl(cmd):
    """A request from a curl command line (the common options)."""
    cmd = re.sub(r"\\\r?\n", " ", (cmd or "").strip())
    try:
        args = shlex.split(cmd)
    except ValueError as e:
        raise ValueError("That curl command couldn't be read: %s" % e)
    if args and args[0] == "curl":
        args = args[1:]
    r = blank_request("Imported from curl")
    method, data, i, get_mode = None, [], 0, False
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a in ("-X", "--request"):
            method, i = nxt.upper(), i + 1
        elif a in ("-H", "--header"):
            if ":" in nxt:
                k, v = nxt.split(":", 1)
                r["headers"].append({"key": k.strip(), "value": v.strip(), "enabled": True})
            i += 1
        elif a in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json"):
            data.append(nxt)
            if a == "--json":
                r["headers"].append({"key": "Content-Type", "value": "application/json", "enabled": True})
            i += 1
        elif a in ("-u", "--user"):
            u, _, pw = nxt.partition(":")
            r["auth"] = {"type": "basic", "username": u, "password": pw}
            i += 1
        elif a in ("-G", "--get"):
            get_mode = True
        elif a == "--url":
            r["url"], i = nxt, i + 1
        elif a in ("-A", "--user-agent"):
            r["headers"].append({"key": "User-Agent", "value": nxt, "enabled": True})
            i += 1
        elif a in ("-b", "--cookie"):
            r["headers"].append({"key": "Cookie", "value": nxt, "enabled": True})
            i += 1
        elif a.startswith("-"):
            if a in ("-o", "--output", "-w", "--write-out", "-m", "--max-time", "--connect-timeout", "-e", "--referer",
                     "--cacert", "--cert", "--key", "-x", "--proxy"):
                i += 1
        elif not r["url"]:
            r["url"] = a
        i += 1
    body = "&".join(data)
    if get_mode and body:
        r["url"] += ("&" if "?" in r["url"] else "?") + body
        body = ""
    if body:
        ctype = next((h["value"] for h in r["headers"] if h["key"].lower() == "content-type"), "")
        looks_json = body.strip()[:1] in ("{", "[")
        r["body"] = {"mode": "json" if ("json" in ctype or looks_json) else "form" if "urlencoded" in ctype or "=" in body
                     and not looks_json and not ctype else "text", "text": body, "form": []}
        if r["body"]["mode"] == "form":
            r["body"]["form"] = [{"key": k, "value": v, "enabled": True} for k, v in urllib.parse.parse_qsl(body, keep_blank_values=True)]
            r["headers"] = [h for h in r["headers"] if h["key"].lower() != "content-type"]
        elif r["body"]["mode"] == "json":
            r["headers"] = [h for h in r["headers"] if h["key"].lower() != "content-type"]
    r["method"] = method or ("POST" if body else "GET")
    if not r["url"]:
        raise ValueError("No URL found in that curl command.")
    path = urllib.parse.urlsplit(r["url"] if "://" in r["url"] else "http://" + r["url"]).path or "/"
    r["name"] = "%s %s" % (r["method"], path)
    return r


def to_curl(req, vals=None, secrets=()):
    b = build(req, vals or {})
    parts = ["curl"]
    if b["method"] != "GET" or b["data"] is not None and b["method"] == "GET":
        parts += ["-X", b["method"]]
    parts.append(shlex.quote(b["url"]))
    for k, v in b["headers"]:
        if k.lower() in ("user-agent", "accept") and v in ("VSCodeAssistant-API/1.0", "*/*"):
            continue
        parts += ["-H", shlex.quote("%s: %s" % (k, v))]
    if b["data"] is not None:
        parts += ["--data-raw", shlex.quote(b["data"].decode("utf-8", "replace"))]
    return mask(" ".join(parts), secrets)


def from_postman(doc):
    """(collection, environment-or-None) from a Postman v2.0/2.1 collection export."""
    if not isinstance(doc, dict) or "item" not in doc:
        raise ValueError("That isn't a Postman collection (it has no 'item' list).")
    info = doc.get("info") or {}
    col = {"id": new_id(), "name": info.get("name") or "Imported collection", "requests": []}

    def kv(lst):
        return [{"key": x.get("key", ""), "value": str(x.get("value", "")), "enabled": not x.get("disabled")}
                for x in lst or [] if isinstance(x, dict)]

    def walk(items, prefix):
        for it in items or []:
            if "item" in it:
                walk(it["item"], prefix + (it.get("name") or "") + " / ")
                continue
            rq = it.get("request") or {}
            if isinstance(rq, str):
                rq = {"url": rq, "method": "GET"}
            url = rq.get("url") or ""
            params = []
            if isinstance(url, dict):
                params = kv(url.get("query"))
                url = url.get("raw") or ""
                url = url.split("?", 1)[0] if params else url
            r = blank_request(prefix + (it.get("name") or "Request"), (rq.get("method") or "GET").upper(), url)
            r["params"] = params
            r["headers"] = kv(rq.get("header") if isinstance(rq.get("header"), list) else [])
            body = rq.get("body") or {}
            mode = body.get("mode")
            if mode == "raw":
                lang = ((body.get("options") or {}).get("raw") or {}).get("language", "")
                raw = body.get("raw") or ""
                r["body"] = {"mode": "json" if lang == "json" or raw.strip()[:1] in ("{", "[") else "text", "text": raw, "form": []}
            elif mode in ("urlencoded", "formdata"):
                r["body"] = {"mode": "form", "text": "", "form": [f for f in kv(body.get(mode)) if f["key"]]}
            auth = rq.get("auth") or {}
            vals = {x.get("key"): x.get("value") for x in auth.get(auth.get("type", ""), []) or [] if isinstance(x, dict)}
            if auth.get("type") == "bearer":
                r["auth"] = {"type": "bearer", "token": str(vals.get("token", ""))}
            elif auth.get("type") == "basic":
                r["auth"] = {"type": "basic", "username": str(vals.get("username", "")), "password": str(vals.get("password", ""))}
            elif auth.get("type") == "apikey":
                r["auth"] = {"type": "apikey", "key": str(vals.get("key", "")), "value": str(vals.get("value", "")),
                             "in": "query" if vals.get("in") == "query" else "header"}
            # pm.response.to.have.status(200) in a test script → a status check
            checks = []
            for ev in it.get("event") or []:
                src = "\n".join((ev.get("script") or {}).get("exec") or [])
                for code in re.findall(r"to\.have\.status\((\d{3})\)", src):
                    checks.append({"kind": "status", "op": "eq", "value": code, "enabled": True})
            r["checks"] = checks or default_checks()
            col["requests"].append(r)
    walk(doc["item"], "")
    env = None
    if doc.get("variable"):
        env = {"id": new_id(), "name": col["name"] + " variables",
               "vars": [{"key": v.get("key", ""), "value": str(v.get("value", "")), "secret": False, "enabled": True}
                        for v in doc["variable"] if isinstance(v, dict) and v.get("key")]}
    return col, env


def to_postman(col):
    items = []
    for r in col["requests"]:
        rq = {"method": r["method"], "header": [{"key": h["key"], "value": h["value"], "disabled": not h.get("enabled", True)}
                                               for h in r.get("headers") or []],
              "url": {"raw": r["url"] + (("?" + "&".join("%s=%s" % (p["key"], p["value"]) for p in r["params"] if p.get("enabled", True)))
                                         if r.get("params") else ""),
                      "query": [{"key": p["key"], "value": p["value"], "disabled": not p.get("enabled", True)} for p in r.get("params") or []]}}
        b = r.get("body") or {}
        if b.get("mode") in ("json", "text"):
            rq["body"] = {"mode": "raw", "raw": b.get("text", ""), "options": {"raw": {"language": "json" if b["mode"] == "json" else "text"}}}
        elif b.get("mode") == "form":
            rq["body"] = {"mode": "urlencoded", "urlencoded": [{"key": f["key"], "value": f["value"]} for f in b.get("form") or []]}
        a = r.get("auth") or {}
        if a.get("type") == "bearer":
            rq["auth"] = {"type": "bearer", "bearer": [{"key": "token", "value": a.get("token", ""), "type": "string"}]}
        elif a.get("type") == "basic":
            rq["auth"] = {"type": "basic", "basic": [{"key": "username", "value": a.get("username", "")},
                                                     {"key": "password", "value": a.get("password", "")}]}
        items.append({"name": r["name"], "request": rq})
    return {"info": {"name": col["name"], "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "item": items}


# ── endpoints in the code, and in the docs ───────────────────────────────
ROUTE_PATTERNS = [
    # Express / Koa / Fastify / Hono:  app.get('/users/:id', …)  router.post("/x")
    ("js", re.compile(r"""\b(?:app|router|server|api|routes?|fastify|hono)\.(get|post|put|patch|delete|options|head|all)\s*\(\s*['"`]([^'"`]+)['"`]""", re.I)),
    # FastAPI / Flask 2 / Starlette:  @app.get("/x")  @router.post("/x")
    ("py", re.compile(r"""@\w+\.(get|post|put|patch|delete|options|head|api_route)\s*\(\s*['"]([^'"]+)['"]""", re.I)),
    # Flask:  @app.route("/x", methods=["GET", "POST"])
    ("py", re.compile(r"""@\w+\.route\s*\(\s*['"]([^'"]+)['"](?:[^)]*methods\s*=\s*\[([^\]]*)\])?""", re.I)),
    # Django:  path("users/<int:id>/", views.x)
    ("py", re.compile(r"""\b(?:re_)?path\s*\(\s*r?['"]([^'"]*)['"]\s*,""")),
    # Spring:  @GetMapping("/x")  @RequestMapping(value = "/x", method = RequestMethod.GET)
    ("java", re.compile(r"""@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*"([^"]*)\"""")),
    # ASP.NET:  [HttpGet("x")]  [Route("api/[controller]")]
    ("cs", re.compile(r"""\[Http(Get|Post|Put|Patch|Delete)\s*(?:\(\s*"([^"]*)"\s*\))?\]""")),
    ("cs", re.compile(r"""app\.Map(Get|Post|Put|Patch|Delete)\s*\(\s*"([^"]+)\"""")),
    # Go: net/http, gin, echo, chi, fiber
    ("go", re.compile(r"""\.(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\s*\(\s*"([^"]+)\"""")),
    ("go", re.compile(r"""\bHandleFunc\s*\(\s*"(?:(GET|POST|PUT|PATCH|DELETE)\s+)?([^"]+)\"""")),
    # Rails routes.rb:  get '/x', to: …
    ("rb", re.compile(r"""^\s*(get|post|put|patch|delete)\s+['"]([^'"]+)['"]""", re.M)),
    # Laravel:  Route::get('/x', …)
    ("php", re.compile(r"""Route::(get|post|put|patch|delete|any)\s*\(\s*['"]([^'"]+)['"]""", re.I)),
]
EXT = {"js": (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"), "py": (".py",), "java": (".java", ".kt"), "cs": (".cs",),
       "go": (".go",), "rb": (".rb",), "php": (".php",)}


def norm_path(p):
    """/users/:id, /users/{id}, /users/<int:id> → /users/{}  (so code and docs compare)."""
    p = (p or "").strip().split("?")[0]
    p = re.sub(r"https?://[^/]+", "", p)
    p = re.sub(r"\{\{[^}]*\}\}", "", p)                      # {{baseUrl}}
    p = re.sub(r":\w+|\{[^}/]*\}|<[^>/]*>|\[[^\]/]*\]|\*\w*", "{}", p)
    p = "/" + p.strip("/")
    return re.sub(r"/+", "/", p).lower()


def find_routes(project):
    """HTTP endpoints defined in the project's code: [{method, path, file, line}]."""
    routes, seen = [], set()
    for rel in project.files():
        ext = os.path.splitext(rel)[1].lower()
        kinds = [k for k, exts in EXT.items() if ext in exts]
        if not kinds or "/node_modules/" in "/" + rel or rel.endswith((".min.js", ".d.ts")):
            continue
        if re.search(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.", rel):
            continue
        try:
            with open(os.path.join(project.root, rel), encoding="utf-8", errors="replace") as f:
                text = f.read(400000)
        except OSError:
            continue
        prefix = ""
        if kinds[0] == "cs":
            m = re.search(r"""\[Route\s*\(\s*"([^"]*)"\s*\)\]""", text)
            if m:
                ctrl = re.search(r"class\s+(\w+?)Controller\b", text)
                prefix = m.group(1).replace("[controller]", ctrl.group(1).lower() if ctrl else "")
        if kinds[0] == "java":
            m = re.search(r"""@RequestMapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]*)"\s*\)\s*(?:public\s+)?class""", text)
            prefix = m.group(1) if m else ""
        for kind, rx in ROUTE_PATTERNS:
            if kind not in kinds:
                continue
            for m in rx.finditer(text):
                g = m.groups()
                if rx.pattern.startswith("@\\w+\\.route"):
                    path, methods = g[0], re.findall(r"['\"](\w+)['\"]", g[1] or "") or ["GET"]
                elif "path\\s*\\(" in rx.pattern:
                    path, methods = "/" + g[0].lstrip("^").rstrip("$"), ["ANY"]
                elif kind == "cs" and "Http" in rx.pattern:
                    path, methods = "/".join(x.strip("/") for x in (prefix, g[1] or "") if x), [g[0]]
                    path = "/" + path
                else:
                    methods, path = [g[0]], g[1]
                    if kind == "java":
                        path = "/" + "/".join(x.strip("/") for x in (prefix, path) if x.strip("/"))
                        if methods[0] == "Request":
                            mm = re.search(r"RequestMethod\.(\w+)", text[m.end():m.end() + 120])
                            methods = [mm.group(1) if mm else "ANY"]
                if kind == "java" and g[0] == "Request" and re.match(r"[^)]*\)\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public\s+|final\s+|abstract\s+)*class\b", text[m.end():]):
                    continue          # the class-level prefix, not a route of its own
                if not path.startswith("/") and kind not in ("py",):
                    if not re.match(r"^[\w{:<\[*-]", path):
                        continue
                    path = "/" + path
                line = text.count("\n", 0, m.start()) + 1
                for meth in methods:
                    meth = meth.upper().replace("API_ROUTE", "ANY").replace("ALL", "ANY")
                    key = (meth, norm_path(path))
                    if key in seen:
                        continue
                    seen.add(key)
                    routes.append({"method": meth, "path": path, "file": rel, "line": line})
    # JAX-RS (Java EE / Jakarta, common in NetBeans projects): @Path on the class and method, @GET etc.
    app_prefix = ""
    jfiles = [f for f in project.files() if f.endswith(".java") and not re.search(r"(^|/)src/test/", f)]
    texts = {}
    for rel in jfiles:
        try:
            with open(os.path.join(project.root, rel), encoding="utf-8", errors="replace") as f:
                texts[rel] = f.read(400000)
        except OSError:
            continue
        m = re.search(r"""@ApplicationPath\s*\(\s*"([^"]*)"\s*\)""", texts[rel])
        if m:
            app_prefix = m.group(1)
    for rel, text in texts.items():
        if "javax.ws.rs" not in text and "jakarta.ws.rs" not in text:
            continue
        cm = re.search(r"""@Path\s*\(\s*"([^"]*)"\s*\)(?:\s*@\w+(?:\([^)]*\))?)*\s*(?:public\s+|final\s+|abstract\s+)*class\b""", text)
        cls = cm.group(1) if cm else ""
        bounds = [b.end() for b in re.finditer(r";|[{}](?=\s)", text)]   # statement/body edges, not {id} in strings
        for m in re.finditer(r"@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b(?!\s*\()", text):
            start = max([b for b in bounds if b <= m.start()] or [0])
            em = re.compile(r"\)\s*(?:throws\s+[\w.,\s]+)?\{").search(text, m.end())
            block = text[start:em.end() if em else m.end() + 300]
            pm = re.search(r"""@Path\s*\(\s*"([^"]*)"\s*\)""", block)
            path = "/" + "/".join(x.strip("/") for x in (app_prefix, cls, pm.group(1) if pm else "") if x.strip("/"))
            key = (m.group(1), norm_path(path))
            if key in seen:
                continue
            seen.add(key)
            routes.append({"method": m.group(1), "path": path, "file": rel, "line": text.count("\n", 0, m.start()) + 1})
    # an OpenAPI file in the repo counts as the source of truth too
    for rel in project.files():
        if re.search(r"(^|/)(openapi|swagger)[^/]*\.json$", rel, re.I):
            try:
                with open(os.path.join(project.root, rel), encoding="utf-8") as f:
                    spec = json.load(f)
                for path, ops in (spec.get("paths") or {}).items():
                    for meth in ops:
                        if meth.upper() in METHODS:
                            key = (meth.upper(), norm_path(path))
                            if key not in seen:
                                seen.add(key)
                                routes.append({"method": meth.upper(), "path": path, "file": rel, "line": 0})
            except (OSError, ValueError, AttributeError):
                pass
    routes.sort(key=lambda r: (norm_path(r["path"]), r["method"]))
    return routes


DOC_ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b`?\s*(?:\|\s*)?`?((?:https?://[^\s`|]+)?/[^\s`|)\]]*)")


def doc_endpoints(text):
    out, seen = [], set()
    for m in DOC_ENDPOINT.finditer(text or ""):
        key = (m.group(1), norm_path(m.group(2)))
        if key not in seen:
            seen.add(key)
            out.append({"method": m.group(1), "path": m.group(2).rstrip(".,;:"), "line": text.count("\n", 0, m.start()) + 1})
    return out


def compare(routes, docs):
    """Endpoints in the code but not the docs, and in the docs but not the code.
    docs: [(file, text)]. A route with method ANY matches any documented method."""
    documented = []
    for f, text in docs:
        for e in doc_endpoints(text):
            documented.append(dict(e, file=f))
    def match(a, b):
        return norm_path(a["path"]) == norm_path(b["path"]) and (a["method"] == b["method"] or "ANY" in (a["method"], b["method"]))
    undocumented = [r for r in routes if not any(match(r, d) for d in documented)]
    stale = [d for d in documented if not any(match(r, d) for r in routes)]
    ok = [r for r in routes if r not in undocumented]
    return {"undocumented": undocumented, "stale": stale, "documented": ok, "doc_count": len(documented)}


# ── the dev server ───────────────────────────────────────────────────────
def guess_server(root):
    """(command, url) to start a project's dev server, from its files."""
    j = lambda *p: os.path.join(root, *p)
    py = "python" if os.name == "nt" else "python3"
    try:
        with open(j("package.json"), encoding="utf-8") as f:
            pkg = json.load(f)
        scripts = pkg.get("scripts") or {}
        deps = dict(pkg.get("dependencies") or {}, **(pkg.get("devDependencies") or {}))
        port = "5173" if "vite" in deps else "3000"
        for s in ("dev", "start", "serve"):
            if s in scripts:
                return ("npm run " + s if s != "start" else "npm start"), "http://localhost:%s" % port
    except (OSError, ValueError, AttributeError):
        pass
    if os.path.exists(j("manage.py")):
        return py + " manage.py runserver", "http://localhost:8000"
    for name in ("main.py", "app.py", "server.py", "api.py", "app/main.py", "src/main.py"):
        try:
            with open(j(*name.split("/")), encoding="utf-8", errors="replace") as f:
                src = f.read(20000)
        except OSError:
            continue
        mod = name[:-3].replace("/", ".")
        if "FastAPI(" in src:
            return "%s -m uvicorn %s:app --reload" % (py, mod), "http://localhost:8000"
        if "Flask(" in src:
            return "%s -m flask --app %s run --debug" % (py, mod), "http://localhost:5000"
    if os.path.exists(j("go.mod")):
        return "go run .", "http://localhost:8080"
    if os.path.exists(j("Cargo.toml")):
        return "cargo run", "http://localhost:8080"
    if os.path.exists(j("Gemfile")) and os.path.exists(j("config", "routes.rb")):
        return "bin/rails server", "http://localhost:3000"
    if os.path.exists(j("artisan")):
        return "php artisan serve", "http://localhost:8000"
    return "", "http://localhost:3000"


class Servers:
    """One dev server per project, started from the app, with its log kept and a readiness check."""

    def __init__(self, publish=lambda ev: None):
        self.publish = publish
        self.procs = {}       # project id -> {"proc", "log": [str], "state", "url", "command", "started"}
        self.lock = threading.Lock()

    def status(self, pid):
        s = self.procs.get(pid)
        if not s:
            return {"state": "stopped", "log": ""}
        if s["state"] in ("starting", "running") and s["proc"].poll() is not None:
            s["state"] = "exited"
        return {"state": s["state"], "url": s["url"], "command": s["command"], "started": s["started"],
                "code": s["proc"].poll(), "log": "".join(s["log"])[-60000:]}

    def start(self, pid, command, url, cwd, wait=60):
        from .projects import spawn, stream
        command = (command or "").strip()
        if not command:
            raise ValueError("Set the command that starts the server, e.g. npm run dev.")
        with self.lock:
            cur = self.procs.get(pid)
            if cur and cur["proc"].poll() is None:
                raise ValueError("The server is already running. Stop it first.")
            proc = spawn(command, cwd)
            s = self.procs[pid] = {"proc": proc, "log": [], "state": "starting", "url": url, "command": command,
                                   "started": time.time()}

        def pump():
            def on_text(t):
                t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", t)
                s["log"].append(t)
                if len(s["log"]) > 4000:
                    del s["log"][:1000]
                self.publish({"type": "server_log", "project": pid, "data": t})
            try:
                stream(proc, on_text)
            finally:
                code = proc.wait()
                if s["state"] != "stopped":
                    s["state"] = "exited"
                self.publish({"type": "server", "project": pid, "state": s["state"], "code": code})
        threading.Thread(target=pump, daemon=True).start()

        def ready():
            end = time.time() + wait
            while time.time() < end and proc.poll() is None and s["state"] == "starting":
                if self.listening(url):
                    s["state"] = "running"
                    self.publish({"type": "server", "project": pid, "state": "running"})
                    return
                time.sleep(0.5)
            if s["state"] == "starting" and proc.poll() is None:
                s["state"] = "running"        # it's up, but the URL didn't answer: let the user see the log
                self.publish({"type": "server", "project": pid, "state": "running", "note": "no answer at %s yet" % url})
        threading.Thread(target=ready, daemon=True).start()
        return self.status(pid)

    @staticmethod
    def listening(url):
        u = urllib.parse.urlsplit(url or "")
        if not u.hostname:
            return False
        try:
            with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=1):
                return True
        except OSError:
            return False

    def wait_ready(self, pid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            st = self.status(pid)
            if st["state"] != "starting":
                return st
            time.sleep(0.3)
        return self.status(pid)

    def stop(self, pid):
        from .projects import kill_tree
        s = self.procs.get(pid)
        if s and s["proc"].poll() is None:
            s["state"] = "stopped"
            kill_tree(s["proc"])
            self.publish({"type": "server", "project": pid, "state": "stopped"})

    def stop_all(self):
        for pid in list(self.procs):
            self.stop(pid)


# ── running a whole collection ───────────────────────────────────────────
def is_safe(req, vals=None):
    """A request that only reads from this computer: no approval needed when the AI sends it."""
    b = build(req, vals or {})
    return b["method"] in SAFE_METHODS and is_local(b["url"])


def run_collection(col, vals, secrets=(), timeout=30, on_result=None):
    """Send every request in order and run its checks. Returns (results, text summary for the Tests list)."""
    results, lines = [], []
    for r in col.get("requests") or []:
        res = send(r, vals, timeout=timeout)
        checks = run_checks(r.get("checks") or default_checks(), res)
        ok = not res.get("error") and all(c["pass"] for c in checks)
        item = {"id": r["id"], "name": r["name"], "method": res.get("method"), "url": mask(res.get("url", ""), secrets),
                "status": res.get("status"), "ms": res.get("ms"), "error": res.get("error", ""), "checks": checks, "pass": ok}
        results.append(item)
        what = "%s %s" % (r["name"], "(%s, %s ms)" % (res.get("status"), res.get("ms")) if not res.get("error") else "(%s)" % res["error"])
        lines.append(("PASS " if ok else "FAIL ") + what)
        for c in checks:
            if not c["pass"]:
                lines.append("    %s: got %s" % (c["label"], c["actual"]))
        if on_result:
            on_result(item)
    passed = sum(1 for x in results if x["pass"])
    lines.append("%d passed, %d failed" % (passed, len(results) - passed) if results else "No requests in this collection.")
    return results, mask("\n".join(lines), secrets)


def pretty_body(res, limit=6000):
    if res.get("json") is not None:
        text = json.dumps(res["json"], indent=2, ensure_ascii=False)
    elif res.get("body_b64"):
        text = "(binary %s, %d bytes)" % (res.get("content_type") or "data", res.get("size") or 0)
    else:
        text = res.get("body") or ""
    return text if len(text) <= limit else text[:limit] + "\n…[%d more characters]" % (len(text) - limit)
