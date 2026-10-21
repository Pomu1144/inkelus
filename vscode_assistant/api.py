"""The API tab: send HTTP requests, keep them in collections, check the responses, and run your dev server.

Requests go out from this app's local server, not the browser, so there are no CORS limits and
localhost / intranet APIs work. Collections, environments and history are saved in api.json in the
app's data folder. A collection belongs to a project or is global.

Environments hold {{variables}}. A variable marked secret is never shown to the AI: the AI writes
{{token}} and the value is filled in here, and it's masked in anything the AI reads back.
"""
import base64
import json
import mimetypes
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
            "body": {"mode": "none", "text": "", "form": []}, "auth": {"type": "inherit"}, "checks": [],
            "pre_script": "", "test_script": "", "settings": {}}


def blank_collection(name, project=""):
    return {"id": new_id(), "name": name, "project": project, "items": [], "auth": {"type": "none"}, "variables": [],
            "pre_script": "", "test_script": "", "description": ""}


def migrate(col):
    """Older collections had a flat "requests" list; now it's a tree of folders and requests."""
    if "items" not in col:
        col["items"] = col.pop("requests", [])
    col.pop("requests", None)
    for k, v in (("auth", {"type": "none"}), ("variables", []), ("pre_script", ""), ("test_script", ""), ("description", "")):
        col.setdefault(k, v)
    return col


# ── the collection tree ─────────────────────────────────────────────────
def walk(items, parents=()):
    """(node, [ancestor folders]) for every folder and request, depth first."""
    for it in items or []:
        yield it, list(parents)
        if it.get("folder"):
            yield from walk(it.get("items"), list(parents) + [it])


def requests_in(node):
    """Every request under a collection or folder, in order."""
    return [n for n, _ in walk(node.get("items")) if not n.get("folder")]


def find(col, item_id):
    """(node, chain) where chain is [collection, folders…, node]; (None, [col]) if absent."""
    for n, parents in walk(col.get("items")):
        if n.get("id") == item_id:
            return n, [col] + parents + [n]
    return None, [col]


def effective_auth(chain):
    """The auth a request really uses: its own, or the nearest folder's or the collection's."""
    for node in reversed(chain):
        a = node.get("auth") or {}
        if a.get("type") and a["type"] != "inherit":
            return a
    return {"type": "none"}


def hashlib_id(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "api.json")
        self.files_dir = os.path.join(data_dir, "api-files")
        self.lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        for k, v in (("collections", []), ("environments", []), ("active_env", {}), ("history", []), ("globals", {}),
                     ("tokens", {}), ("settings", {})):
            if not isinstance(self.data.get(k), type(v)):
                self.data[k] = v
        for c in self.data["collections"]:
            migrate(c)
        self.jars = {}
        self.shared = {}          # collection id -> collection loaded from a project's .api folder
        self.shared_files = {}    # file path -> (mtime, collection)

    def save(self):
        with self.lock:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)       # environments and tokens can be secret
            except OSError:
                pass

    def settings(self):
        s = {"timeout_ms": 30000, "follow": True, "verify": True, "proxy": "", "cert": "", "key": "", "cookies": True}
        s.update(self.data.get("settings") or {})
        return s

    def scope(self, project, root=None):
        """Collections and environments for a project ("" = global), plus the global ones."""
        p = project or ""
        cols = [c for c in self.data["collections"] if c.get("project", "") in (p, "")]
        if root:
            cols = self.load_shared(p, root) + cols
        envs = [e for e in self.data["environments"] if e.get("project", "") in (p, "")]
        hist = [h for h in self.data["history"] if h.get("project", "") == p][:MAX_HISTORY]
        return {"collections": cols, "environments": envs, "active_env": self.data["active_env"].get(p, ""),
                "history": hist, "globals": self.data["globals"].get(p, []), "settings": self.settings()}

    def collection(self, cid):
        return next((c for c in self.data["collections"] if c["id"] == cid), None) or self.shared.get(cid)

    # collections kept in a project's .api folder (Postman format), so the team gets them through Git
    def load_shared(self, project, root):
        folder = os.path.join(root, ".api")
        out = []
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".postman_collection.json"))
        except OSError:
            names = []
        for n in names:
            path = os.path.join(folder, n)
            try:
                mtime = os.path.getmtime(path)
                cached = self.shared_files.get(path)
                if cached and cached[0] == mtime:
                    col = cached[1]
                else:
                    with open(path, encoding="utf-8") as f:
                        doc = json.load(f)
                    col = from_postman(doc)[0]
                    pid = (doc.get("info") or {}).get("_postman_id") or ""
                    col["id"] = pid if re.fullmatch(r"[0-9a-f]{12}", pid) else hashlib_id(path)
                    self.shared_files[path] = (mtime, col)
                col.update(project=project, location="project", file=os.path.relpath(path, root).replace(os.sep, "/"), root=root)
                self.shared[col["id"]] = col
                out.append(col)
            except (OSError, ValueError) as e:
                out.append({"id": hashlib_id(path), "name": n, "project": project, "location": "project", "items": [],
                            "error": "Couldn't read %s: %s" % (n, e), "auth": {"type": "none"}, "variables": []})
        return out

    def persist(self, col):
        """Save a collection where it lives: the app's data, or its file in the project."""
        if col.get("location") == "project" and col.get("root"):
            path = os.path.join(col["root"], *col["file"].split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            doc = to_postman(col)
            doc["info"]["_postman_id"] = col["id"]
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
                f.write("\n")
            self.shared_files[path] = (os.path.getmtime(path), col)
        else:
            self.save()

    def move(self, col, to, root=None):
        """Move a collection into the project's .api folder (shared through Git) or back into the app."""
        with self.lock:
            if to == "project":
                if not root:
                    raise ValueError("Pick a project first.")
                slug = re.sub(r"[^\w.-]+", "-", col["name"]).strip("-.") or "collection"
                col.update(location="project", root=root, file=".api/%s.postman_collection.json" % slug)
                self.persist(col)
                self.data["collections"] = [c for c in self.data["collections"] if c["id"] != col["id"]]
                self.shared[col["id"]] = col
                self.save()
            else:
                path = os.path.join(col["root"], *col["file"].split("/")) if col.get("root") else None
                for k in ("location", "file", "root"):
                    col.pop(k, None)
                self.shared.pop(col["id"], None)
                self.data["collections"].append(col)
                self.save()
                if path and os.path.exists(path):
                    os.remove(path)
                    self.shared_files.pop(path, None)
        return col

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

    # cookies: one jar per project, kept between runs like a browser
    def jar(self, project):
        import http.cookiejar
        p = project or "_global"
        if p not in self.jars:
            os.makedirs(os.path.join(self.dir, "cookies"), exist_ok=True)
            j = http.cookiejar.LWPCookieJar(os.path.join(self.dir, "cookies", re.sub(r"\W", "_", p) + ".txt"))
            try:
                j.load(ignore_discard=True)
            except (OSError, http.cookiejar.LoadError):
                pass
            self.jars[p] = j
        return self.jars[p]

    def save_jar(self, project):
        j = self.jars.get(project or "_global")
        if j is not None:
            try:
                j.save(ignore_discard=True)
            except OSError:
                pass

    # files attached to form-data and binary bodies
    def add_file(self, name, data):
        os.makedirs(self.files_dir, exist_ok=True)
        fid = new_id()
        with open(os.path.join(self.files_dir, fid), "wb") as f:
            f.write(data)
        return {"id": fid, "name": os.path.basename(name or "file"), "size": len(data)}

    def read_file(self, fid):
        if not re.fullmatch(r"[0-9a-f]{12}", fid or ""):
            raise ValueError("Unknown file.")
        try:
            with open(os.path.join(self.files_dir, fid), "rb") as f:
                return f.read()
        except OSError:
            raise ValueError("The file attached to this request is gone; choose it again.")


# ── variables ────────────────────────────────────────────────────────────
VAR = re.compile(r"\{\{\s*([\w.$-]+)\s*\}\}")


def kv_dict(rows):
    return {r["key"]: str(r.get("value") or "") for r in rows or [] if r.get("enabled", True) and r.get("key")}


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
        if k in ("$uuid", "$guid", "$randomUUID"):
            return str(uuid.uuid4())
        if k == "$randomInt":
            return str(int.from_bytes(os.urandom(2), "big") % 1000)
        if k == "$randomEmail":
            return "user%d@example.com" % (int.from_bytes(os.urandom(2), "big") % 100000)
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
class NeedsToken(Exception):
    """OAuth 2.0 authorization code flow: the user has to sign in in a browser window first."""


def build(req, vals, auth=None, store=None, files=None):
    """The concrete request: method, URL (with params), headers and body bytes, with auth applied.
    auth: the effective auth (inherited); defaults to the request's own. store: for OAuth tokens and files."""
    missing = set()
    f = lambda s: fill(s, vals, missing)
    method = (req.get("method") or "GET").upper()
    url = f(req.get("url", "")).strip()
    pv = {p.get("key"): f(p.get("value")) for p in req.get("path_vars") or [] if p.get("key")}
    if pv:      # Postman-style path variables: /users/:id
        url = re.sub(r"(?<=/):([A-Za-z_]\w*)", lambda m: urllib.parse.quote(pv[m.group(1)], safe="") if m.group(1) in pv else m.group(0), url)
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    pairs = [(f(p.get("key")), f(p.get("value"))) for p in req.get("params") or [] if p.get("enabled", True) and p.get("key")]
    headers = [(f(h["key"]).strip(), f(h.get("value"))) for h in req.get("headers") or [] if h.get("enabled", True) and h.get("key")]
    auth = auth if auth is not None else (req.get("auth") or {})
    t = auth.get("type", "none")
    digest = None
    if t == "bearer" and auth.get("token"):
        headers.append(("Authorization", "Bearer " + f(auth["token"])))
    elif t == "basic":
        raw = "%s:%s" % (f(auth.get("username")), f(auth.get("password")))
        headers.append(("Authorization", "Basic " + base64.b64encode(raw.encode()).decode()))
    elif t == "apikey" and auth.get("key"):
        k, v = f(auth["key"]), f(auth.get("value"))
        (pairs if auth.get("in") == "query" else headers).append((k, v))
    elif t == "digest":
        digest = (f(auth.get("username")), f(auth.get("password")))
    elif t == "oauth2":
        tok = oauth_token(store, auth, vals) if store is not None else None
        if tok:
            if auth.get("add_to") == "query":
                pairs.append(("access_token", tok))
            else:
                headers.append(("Authorization", (f(auth.get("prefix")) or "Bearer") + " " + tok))
    if pairs:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)
    body = req.get("body") or {}
    mode = body.get("mode", "none")
    data = None
    has = {k.lower() for k, _ in headers}

    def ctype(v):
        if "content-type" not in has:
            headers.append(("Content-Type", v))
    if mode == "json":
        data = f(body.get("text")).encode()
        ctype("application/json")
    elif mode == "text":
        data = f(body.get("text")).encode()
        ctype(body.get("content_type") or "text/plain; charset=utf-8")
    elif mode == "xml":
        data = f(body.get("text")).encode()
        ctype("application/xml")
    elif mode == "form":
        data = urllib.parse.urlencode([(f(x.get("key")), f(x.get("value"))) for x in body.get("form") or []
                                       if x.get("enabled", True) and x.get("key")]).encode()
        ctype("application/x-www-form-urlencoded")
    elif mode == "multipart":
        boundary = "----VSCodeAssistant" + uuid.uuid4().hex
        parts = []
        for x in body.get("form") or []:
            if not x.get("enabled", True) or not x.get("key"):
                continue
            name = f(x["key"]).replace('"', "%22")
            if x.get("type") == "file":
                if not (x.get("file") or {}).get("id"):
                    continue
                content = (files or (lambda i: store.read_file(i)))(x["file"]["id"])
                fname = (x["file"].get("name") or "file").replace('"', "%22")
                ct = x.get("content_type") or mimetypes.guess_type(fname)[0] or "application/octet-stream"
                parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                              % (boundary, name, fname, ct)).encode() + content + b"\r\n")
            else:
                parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n' % (boundary, name)).encode()
                             + f(x.get("value")).encode() + b"\r\n")
        data = b"".join(parts) + ("--%s--\r\n" % boundary).encode()
        headers = [(k, v) for k, v in headers if k.lower() != "content-type"]
        headers.append(("Content-Type", "multipart/form-data; boundary=" + boundary))
    elif mode == "binary":
        fid = (body.get("file") or {}).get("id")
        if fid:
            data = (files or (lambda i: store.read_file(i)))(fid)
            ctype(mimetypes.guess_type(body["file"].get("name") or "")[0] or "application/octet-stream")
    elif mode == "graphql":
        variables_text = f(body.get("variables") or "").strip()
        try:
            gvars = json.loads(variables_text) if variables_text else {}
        except ValueError:
            raise ValueError("The GraphQL variables aren't valid JSON.")
        data = json.dumps({"query": f(body.get("query")), "variables": gvars}).encode()
        ctype("application/json")
    if "user-agent" not in has:
        headers.append(("User-Agent", "VSCodeAssistant-API/1.0"))
    if "accept" not in has:
        headers.append(("Accept", "*/*"))
    if t == "awsv4":
        headers = sigv4(method, url, headers, data or b"", {k: f(auth.get(k)) for k in
                        ("access_key", "secret_key", "session_token", "region", "service")})
    return {"method": method, "url": url, "headers": headers, "data": data, "missing": sorted(missing), "digest": digest}


def sigv4(method, url, headers, payload, cfg):
    """AWS Signature Version 4: adds X-Amz-Date, x-amz-content-sha256 and Authorization."""
    import hashlib
    import hmac
    if not cfg.get("access_key") or not cfg.get("secret_key"):
        raise ValueError("AWS signature: set the access key and secret key.")
    u = urllib.parse.urlsplit(url)
    region = cfg.get("region") or "us-east-1"
    service = cfg.get("service") or "execute-api"
    now = time.gmtime()
    amz_date, day = time.strftime("%Y%m%dT%H%M%SZ", now), time.strftime("%Y%m%d", now)
    phash = hashlib.sha256(payload).hexdigest()
    hdrs = [(k, v) for k, v in headers if k.lower() not in ("x-amz-date", "x-amz-content-sha256", "authorization")]
    hdrs += [("X-Amz-Date", amz_date), ("x-amz-content-sha256", phash)]
    if cfg.get("session_token"):
        hdrs.append(("X-Amz-Security-Token", cfg["session_token"]))
    canon = {"host": u.netloc}
    for k, v in hdrs:
        if k.lower() not in ("user-agent", "accept"):
            canon[k.lower()] = " ".join(str(v).split())
    signed = ";".join(sorted(canon))
    q = sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    cq = "&".join("%s=%s" % (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~")) for k, v in q)
    creq = "\n".join([method, urllib.parse.quote(u.path or "/", safe="/-_.~"), cq,
                      "".join("%s:%s\n" % (k, canon[k]) for k in sorted(canon)), signed, phash])
    scope = "%s/%s/%s/aws4_request" % (day, region, service)
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    key = ("AWS4" + cfg["secret_key"]).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    hdrs.append(("Authorization", "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                 % (cfg["access_key"], scope, signed, sig)))
    return hdrs


# ── OAuth 2.0 ────────────────────────────────────────────────────────────
def _token_key(auth, vals):
    import hashlib
    parts = [fill(auth.get(k), vals) for k in ("grant", "token_url", "client_id", "scope", "audience", "username")]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:24]


def token_request(auth, vals, form):
    """POST to the token URL; returns the token response JSON."""
    f = lambda s: fill(s, vals)
    url = f(auth.get("token_url"))
    if not url:
        raise ValueError("OAuth 2.0: set the access token URL.")
    form = dict(form)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    cid, secret = f(auth.get("client_id")), f(auth.get("client_secret"))
    if auth.get("client_auth") == "body":
        form.update(client_id=cid, **({"client_secret": secret} if secret else {}))
    elif cid:
        headers["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % (urllib.parse.quote(cid), urllib.parse.quote(secret))).encode()).decode()
    if f(auth.get("scope")):
        form.setdefault("scope", f(auth.get("scope")))
    if f(auth.get("audience")):
        form.setdefault("audience", f(auth.get("audience")))
    r = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST", headers=headers)
    opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if is_local(url) else []))
    try:
        with opener.open(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ValueError("The token server said %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    except (urllib.error.URLError, OSError) as e:
        raise ValueError("Couldn't reach the token server: %s" % getattr(e, "reason", e))
    try:
        j = json.loads(raw)
    except ValueError:
        j = dict(urllib.parse.parse_qsl(raw))
    if not j.get("access_token"):
        raise ValueError("The token server didn't return an access token: %s" % raw[:300])
    return j


def store_token(store, auth, vals, j):
    tok = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""),
           "expires_at": time.time() + float(j.get("expires_in") or 3600), "token_type": j.get("token_type", "Bearer"),
           "scope": j.get("scope", ""), "got": time.time()}
    with store.lock:
        store.data["tokens"][_token_key(auth, vals)] = tok
        store.save()
    return tok


def oauth_token(store, auth, vals, force=False):
    """A valid access token for this OAuth 2.0 config: cached, refreshed, or fetched (client credentials /
    password). The authorization-code grant needs the user to sign in first (NeedsToken)."""
    key = _token_key(auth, vals)
    tok = store.data["tokens"].get(key)
    if tok and not force and tok.get("expires_at", 0) - 30 > time.time():
        return tok["access_token"]
    if tok and tok.get("refresh_token"):
        try:
            return store_token(store, auth, vals, token_request(auth, vals, {"grant_type": "refresh_token",
                                                                             "refresh_token": tok["refresh_token"]}))["access_token"]
        except ValueError:
            pass
    grant = auth.get("grant") or "client_credentials"
    if grant == "client_credentials":
        j = token_request(auth, vals, {"grant_type": "client_credentials"})
    elif grant == "password":
        j = token_request(auth, vals, {"grant_type": "password", "username": fill(auth.get("username"), vals),
                                       "password": fill(auth.get("password"), vals)})
    else:
        raise NeedsToken("This request uses OAuth 2.0 sign-in. Press \"Get new token\" in its Auth tab and sign in first.")
    return store_token(store, auth, vals, j)["access_token"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def is_local(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost") or net.is_local(host)


def send(req, vals, timeout=30, follow=True, verify=True, auth=None, store=None, project=None, settings=None):
    """Send one request. Never raises for HTTP errors: a 404 or 500 is a normal result.
    settings (optional): timeout_ms, follow, verify, proxy, cert, key, cookies."""
    s = dict(settings or {})
    timeout = float(s.get("timeout_ms") or timeout * 1000) / 1000
    follow = s.get("follow", follow) is not False
    verify = s.get("verify", verify) is not False
    out = {}
    try:
        b = build(req, vals, auth, store)
    except NeedsToken as e:
        return {"method": req.get("method"), "url": req.get("url"), "error": str(e), "needs_token": True, "missing": []}
    except ValueError as e:
        return {"method": req.get("method"), "url": req.get("url"), "error": str(e), "missing": []}
    out.update(method=b["method"], url=b["url"], missing=b["missing"], sent_headers=b["headers"])
    if not b["url"] or not urllib.parse.urlsplit(b["url"]).hostname:
        out["error"] = "Enter a URL, e.g. http://localhost:3000/api/users"
        return out
    handlers = []
    if s.get("proxy"):
        handlers.append(urllib.request.ProxyHandler({"http": s["proxy"], "https": s["proxy"]}))
    elif is_local(b["url"]):
        handlers.append(urllib.request.ProxyHandler({}))   # skip corporate proxies for local addresses
    if not follow:
        handlers.append(_NoRedirect())
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if s.get("cert"):
        try:
            ctx.load_cert_chain(s["cert"], s.get("key") or None, s.get("passphrase") or None)
        except (OSError, ssl.SSLError) as e:
            out["error"] = "Couldn't load the client certificate: %s" % e
            return out
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    jar = store.jar(project) if store is not None and s.get("cookies", True) is not False else None
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    if b["digest"]:
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, b["url"], b["digest"][0], b["digest"][1])
        handlers.append(urllib.request.HTTPDigestAuthHandler(pm))
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
            msg = "No answer within %g seconds." % timeout
        elif "certificate" in msg.lower():
            msg += " (turn off certificate checks in Settings for self-signed local servers)"
        out.update(error=msg, ms=int((time.time() - t0) * 1000))
        return out
    try:
        raw = resp.read(MAX_BODY + 1)
    except (OSError, socket.timeout) as e:
        raw = b""
        out["error"] = "The response was cut off: %s" % e
    ms = int((time.time() - t0) * 1000)
    if jar is not None:
        store.save_jar(project)
    truncated = len(raw) > MAX_BODY
    raw = raw[:MAX_BODY]
    hdrs = list(resp.headers.items()) if resp.headers else []
    ctype = next((v for k, v in hdrs if k.lower() == "content-type"), "")
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    binary = b"\0" in raw[:2048] or re.match(r"(image|audio|video|font)/|application/(octet-stream|pdf|zip)", ctype, re.I)
    cookies = []
    for k, v in hdrs:
        if k.lower() == "set-cookie":
            name, _, rest = v.partition("=")
            cookies.append({"name": name.strip(), "value": rest.split(";", 1)[0], "raw": v})
    out.update(status=getattr(resp, "status", None) or resp.getcode(), reason=getattr(resp, "reason", "") or "",
               headers=hdrs, size=len(raw), ms=ms, final_url=resp.geturl() if hasattr(resp, "geturl") else b["url"],
               content_type=ctype, truncated=truncated, cookies=cookies)
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
            elif kind == "schema":
                doc = res.get("json")
                if doc is None:
                    raise KeyError("not JSON")
                problems = validate_schema(doc, c.get("schema") or {})
                ok = not problems
                actual = "matches" if ok else "; ".join(problems[:3]) + (" (+%d more)" % (len(problems) - 3) if len(problems) > 3 else "")
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
    if kind == "schema":
        return "Body matches the response schema"
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


PM_AUTH_IN = {"noauth": "none", "bearer": "bearer", "basic": "basic", "apikey": "apikey", "digest": "digest",
              "oauth2": "oauth2", "awsv4": "awsv4"}
PM_GRANT = {"client_credentials": "client_credentials", "password_credentials": "password",
            "authorization_code": "authorization_code", "authorization_code_with_pkce": "authorization_code"}


def _pm_auth_in(a):
    if not isinstance(a, dict) or not a.get("type"):
        return {"type": "inherit"}
    t = a["type"]
    vals = {x.get("key"): x.get("value") for x in a.get(t, []) or [] if isinstance(x, dict)} if isinstance(a.get(t), list) \
        else dict(a.get(t) or {}) if isinstance(a.get(t), dict) else {}
    s = lambda k, d="": "" if vals.get(k) is None else str(vals.get(k, d))
    if t == "bearer":
        return {"type": "bearer", "token": s("token")}
    if t == "basic" or t == "digest":
        return {"type": t, "username": s("username"), "password": s("password")}
    if t == "apikey":
        return {"type": "apikey", "key": s("key"), "value": s("value"), "in": "query" if vals.get("in") == "query" else "header"}
    if t == "awsv4":
        return {"type": "awsv4", "access_key": s("accessKey"), "secret_key": s("secretKey"), "session_token": s("sessionToken"),
                "region": s("region"), "service": s("service")}
    if t == "oauth2":
        return {"type": "oauth2", "grant": PM_GRANT.get(s("grant_type"), "authorization_code"), "token_url": s("accessTokenUrl"),
                "auth_url": s("authUrl"), "client_id": s("clientId"), "client_secret": s("clientSecret"), "scope": s("scope"),
                "username": s("username"), "password": s("password"), "pkce": s("grant_type") == "authorization_code_with_pkce",
                "add_to": "query" if vals.get("addTokenTo") == "queryParams" else "header", "prefix": s("headerPrefix") or "Bearer",
                "client_auth": "body" if vals.get("client_authentication") == "body" else "header", "audience": s("audience")}
    return {"type": PM_AUTH_IN.get(t, "none")}


def _pm_auth_out(a):
    t = (a or {}).get("type", "inherit")
    kv = lambda d: [{"key": k, "value": v, "type": "string"} for k, v in d.items()]
    if t == "inherit":
        return None
    if t == "none":
        return {"type": "noauth"}
    if t == "bearer":
        return {"type": "bearer", "bearer": kv({"token": a.get("token", "")})}
    if t in ("basic", "digest"):
        return {"type": t, t: kv({"username": a.get("username", ""), "password": a.get("password", "")})}
    if t == "apikey":
        return {"type": "apikey", "apikey": kv({"key": a.get("key", ""), "value": a.get("value", ""), "in": a.get("in", "header")})}
    if t == "awsv4":
        return {"type": "awsv4", "awsv4": kv({"accessKey": a.get("access_key", ""), "secretKey": a.get("secret_key", ""),
                                               "sessionToken": a.get("session_token", ""), "region": a.get("region", ""),
                                               "service": a.get("service", "")})}
    if t == "oauth2":
        g = a.get("grant", "client_credentials")
        grant = {"client_credentials": "client_credentials", "password": "password_credentials"}.get(
            g, "authorization_code_with_pkce" if a.get("pkce") else "authorization_code")
        return {"type": "oauth2", "oauth2": kv({"grant_type": grant, "accessTokenUrl": a.get("token_url", ""), "authUrl": a.get("auth_url", ""),
                                                 "clientId": a.get("client_id", ""), "clientSecret": a.get("client_secret", ""),
                                                 "scope": a.get("scope", ""), "username": a.get("username", ""), "password": a.get("password", ""),
                                                 "addTokenTo": "queryParams" if a.get("add_to") == "query" else "header",
                                                 "headerPrefix": a.get("prefix", "Bearer"),
                                                 "client_authentication": "body" if a.get("client_auth") == "body" else "header"})}
    return None


def _pm_events_in(events):
    pre, test = [], []
    for ev in events or []:
        src = (ev.get("script") or {}).get("exec") or []
        code = "\n".join(src) if isinstance(src, list) else str(src)
        (pre if ev.get("listen") == "prerequest" else test if ev.get("listen") == "test" else []).append(code)
    return "\n".join(x for x in pre if x.strip()), "\n".join(x for x in test if x.strip())


def _pm_events_out(node):
    ev = []
    for listen, k in (("prerequest", "pre_script"), ("test", "test_script")):
        if (node.get(k) or "").strip():
            ev.append({"listen": listen, "script": {"type": "text/javascript", "exec": node[k].split("\n")}})
    return ev


def from_postman(doc):
    """(collection, environment-or-None) from a Postman v2.0/2.1 collection export, keeping folders,
    scripts, auth, variables, bodies (incl. form-data, files, GraphQL), path variables and examples."""
    if not isinstance(doc, dict) or "item" not in doc:
        raise ValueError("That isn't a Postman collection (it has no 'item' list).")
    info = doc.get("info") or {}
    col = blank_collection(info.get("name") or "Imported collection")
    col["description"] = info.get("description") if isinstance(info.get("description"), str) else ""
    col["auth"] = _pm_auth_in(doc.get("auth"))
    if col["auth"]["type"] == "inherit":
        col["auth"] = {"type": "none"}
    col["pre_script"], col["test_script"] = _pm_events_in(doc.get("event"))
    col["variables"] = [{"key": v.get("key", ""), "value": "" if v.get("value") is None else str(v.get("value")),
                         "enabled": not v.get("disabled")} for v in doc.get("variable") or [] if isinstance(v, dict) and v.get("key")]

    def kv(lst):
        return [{"key": x.get("key", ""), "value": "" if x.get("value") is None else str(x.get("value")), "enabled": not x.get("disabled")}
                for x in lst or [] if isinstance(x, dict)]

    def item(it):
        ext = it.get("_vscode_assistant") or {}
        if "item" in it:
            f = {"id": ext.get("id") or new_id(), "name": it.get("name") or "Folder", "folder": True, "items": [],
                 "auth": _pm_auth_in(it.get("auth")), "description": it.get("description") if isinstance(it.get("description"), str) else ""}
            f["pre_script"], f["test_script"] = _pm_events_in(it.get("event"))
            f["items"] = [item(x) for x in it["item"] or []]
            return f
        rq = it.get("request") or {}
        if isinstance(rq, str):
            rq = {"url": rq, "method": "GET"}
        url = rq.get("url") or ""
        params, path_vars = [], []
        if isinstance(url, dict):
            params, path_vars = kv(url.get("query")), kv(url.get("variable"))
            raw = url.get("raw") or ""
            url = raw.split("?", 1)[0] if url.get("query") is not None else raw
        r = blank_request(it.get("name") or "Request", (rq.get("method") or "GET").upper(), url)
        r["id"] = ext.get("id") or r["id"]
        r["params"], r["path_vars"] = params, path_vars
        r["headers"] = kv(rq.get("header") if isinstance(rq.get("header"), list) else [])
        r["auth"] = _pm_auth_in(rq.get("auth"))
        r["description"] = rq.get("description") if isinstance(rq.get("description"), str) else ""
        body = rq.get("body") or {}
        mode = body.get("mode")
        if mode == "raw":
            lang = ((body.get("options") or {}).get("raw") or {}).get("language", "")
            raw = body.get("raw") or ""
            m = "json" if lang == "json" or (not lang and raw.strip()[:1] in ("{", "[")) else "xml" if lang == "xml" else "text"
            r["body"] = {"mode": m, "text": raw, "form": []}
        elif mode == "urlencoded":
            r["body"] = {"mode": "form", "text": "", "form": [f for f in kv(body.get("urlencoded")) if f["key"]]}
        elif mode == "formdata":
            rows = []
            for x in body.get("formdata") or []:
                row = {"key": x.get("key", ""), "value": "" if x.get("value") is None else str(x.get("value")), "enabled": not x.get("disabled"),
                       "type": "file" if x.get("type") == "file" else "text"}
                if row["type"] == "file":
                    src = x.get("src") if isinstance(x.get("src"), str) else ""
                    row["file"] = (x.get("_vscode_assistant") or {}).get("file") or {"name": os.path.basename(src) if src else "", "id": ""}
                rows.append(row)
            r["body"] = {"mode": "multipart", "text": "", "form": rows}
        elif mode == "file":
            r["body"] = {"mode": "binary", "text": "", "form": [], "file": ext.get("file") or {"id": "", "name": os.path.basename((body.get("file") or {}).get("src") or "")}}
        elif mode == "graphql":
            g = body.get("graphql") or {}
            r["body"] = {"mode": "graphql", "text": "", "form": [], "query": g.get("query", ""), "variables": g.get("variables", "") or ""}
        r["pre_script"], r["test_script"] = _pm_events_in(it.get("event"))
        pb = it.get("protocolProfileBehavior") or {}
        r["settings"] = dict(ext.get("settings") or {})
        if pb.get("followRedirects") is False:
            r["settings"]["follow"] = False
        if pb.get("strictSSL") is False:
            r["settings"]["verify"] = False
        r["checks"] = ext.get("checks") if isinstance(ext.get("checks"), list) else []
        r["examples"] = []
        for ex in it.get("response") or []:
            if isinstance(ex, dict):
                r["examples"].append({"name": ex.get("name") or "Example", "status": ex.get("code"), "reason": ex.get("status", ""),
                                      "headers": [[h.get("key"), h.get("value")] for h in ex.get("header") or [] if isinstance(h, dict)],
                                      "body": ex.get("body") or ""})
        return r
    col["items"] = [item(x) for x in doc["item"]]
    env = None
    return col, env


def to_postman(col):
    """A Postman v2.1 collection: Postman (and Newman) can open it; checks are kept in an extension field."""
    def url_obj(r):
        q = [{"key": p["key"], "value": p["value"], **({"disabled": True} if p.get("enabled") is False else {})} for p in r.get("params") or []]
        raw = r.get("url", "") + (("?" + "&".join("%s=%s" % (p["key"], p["value"]) for p in r.get("params") or [] if p.get("enabled", True))) if q else "")
        u = {"raw": raw}
        if q:
            u["query"] = q
        if r.get("path_vars"):
            u["variable"] = [{"key": p["key"], "value": p["value"]} for p in r["path_vars"]]
        return u

    def item(n):
        if n.get("folder"):
            out = {"name": n["name"], "item": [item(x) for x in n.get("items") or []], "_vscode_assistant": {"id": n["id"]}}
            a = _pm_auth_out(n.get("auth"))
            if a:
                out["auth"] = a
            if _pm_events_out(n):
                out["event"] = _pm_events_out(n)
            if n.get("description"):
                out["description"] = n["description"]
            return out
        rq = {"method": n["method"], "header": [{"key": h["key"], "value": h["value"], **({"disabled": True} if h.get("enabled") is False else {})}
                                                for h in n.get("headers") or []], "url": url_obj(n)}
        b = n.get("body") or {}
        m = b.get("mode")
        if m in ("json", "text", "xml"):
            rq["body"] = {"mode": "raw", "raw": b.get("text", ""), "options": {"raw": {"language": m}}}
        elif m == "form":
            rq["body"] = {"mode": "urlencoded", "urlencoded": [{"key": f["key"], "value": f["value"]} for f in b.get("form") or []]}
        elif m == "multipart":
            rq["body"] = {"mode": "formdata", "formdata": [
                {"key": f["key"], "type": "file", "src": (f.get("file") or {}).get("name", ""), "_vscode_assistant": {"file": f.get("file")}}
                if f.get("type") == "file" else {"key": f["key"], "value": f.get("value", ""), "type": "text"} for f in b.get("form") or []]}
        elif m == "binary":
            rq["body"] = {"mode": "file", "file": {"src": (b.get("file") or {}).get("name", "")}}
        elif m == "graphql":
            rq["body"] = {"mode": "graphql", "graphql": {"query": b.get("query", ""), "variables": b.get("variables", "")}}
        a = _pm_auth_out(n.get("auth"))
        if a:
            rq["auth"] = a
        if n.get("description"):
            rq["description"] = n["description"]
        out = {"name": n["name"], "request": rq,
               "_vscode_assistant": {"id": n["id"], "checks": n.get("checks") or [], "settings": n.get("settings") or {},
                                     **({"file": b.get("file")} if m == "binary" else {})}}
        if _pm_events_out(n):
            out["event"] = _pm_events_out(n)
        s = n.get("settings") or {}
        pb = {}
        if s.get("follow") is False:
            pb["followRedirects"] = False
        if s.get("verify") is False:
            pb["strictSSL"] = False
        if pb:
            out["protocolProfileBehavior"] = pb
        if n.get("examples"):
            out["response"] = [{"name": e.get("name", "Example"), "code": e.get("status"), "status": e.get("reason", ""),
                                "header": [{"key": k, "value": v} for k, v in e.get("headers") or []], "body": e.get("body", ""),
                                "originalRequest": rq} for e in n["examples"]]
        return out
    out = {"info": {"name": col["name"], "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                    **({"description": col["description"]} if col.get("description") else {})},
           "item": [item(x) for x in col.get("items") or []]}
    a = _pm_auth_out(col.get("auth"))
    if a and a.get("type") != "noauth":
        out["auth"] = a
    if _pm_events_out(col):
        out["event"] = _pm_events_out(col)
    if col.get("variables"):
        out["variable"] = [{"key": v["key"], "value": v.get("value", ""), **({"disabled": True} if v.get("enabled") is False else {})}
                           for v in col["variables"]]
    return out


def env_from_postman(doc):
    """An environment from a Postman environment export."""
    vals = doc.get("values") if isinstance(doc, dict) else None
    if not isinstance(vals, list):
        raise ValueError("That isn't a Postman environment.")
    return {"id": new_id(), "name": doc.get("name") or "Imported environment",
            "vars": [{"key": v.get("key", ""), "value": "" if v.get("value") is None else str(v.get("value")),
                      "secret": v.get("type") == "secret", "enabled": v.get("enabled", True) is not False}
                     for v in vals if isinstance(v, dict) and v.get("key")]}


def env_to_postman(env, include_secrets=False):
    return {"name": env["name"], "_postman_variable_scope": "environment",
            "values": [{"key": v["key"], "value": "" if v.get("secret") and not include_secrets else v.get("value", ""),
                        "type": "secret" if v.get("secret") else "default", "enabled": v.get("enabled", True)} for v in env.get("vars") or []]}


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


def pretty_body(res, limit=6000):
    if res.get("json") is not None:
        text = json.dumps(res["json"], indent=2, ensure_ascii=False)
    elif res.get("body_b64"):
        text = "(binary %s, %d bytes)" % (res.get("content_type") or "data", res.get("size") or 0)
    else:
        text = res.get("body") or ""
    return text if len(text) <= limit else text[:limit] + "\n…[%d more characters]" % (len(text) - limit)


# ── code snippets ────────────────────────────────────────────────────────
def codegen(lang, req, vals, auth=None, store=None):
    """The request as code: curl, python, javascript, java, csharp, go, powershell."""
    try:
        b = build(req, vals, auth, store)
    except NeedsToken:
        b = build(dict(req, auth={"type": "none"}), vals, {"type": "none"}, store)
    hdrs = [(k, v) for k, v in b["headers"] if not (k == "User-Agent" and v.startswith("VSCodeAssistant")) and not (k == "Accept" and v == "*/*")]
    body = b["data"].decode("utf-8", "replace") if b["data"] is not None else None
    binary = (req.get("body") or {}).get("mode") in ("multipart", "binary")
    m, url = b["method"], b["url"]
    js = lambda s: json.dumps(s, ensure_ascii=False)
    if lang == "curl":
        return to_curl(req, vals) if not auth else " ".join(["curl"] + (["-X", m] if m != "GET" else []) + [shlex.quote(url)] +
                                                        sum([["-H", shlex.quote("%s: %s" % kv)] for kv in hdrs], []) +
                                                        (["--data-raw", shlex.quote(body)] if body is not None and not binary else []))
    if lang == "python":
        lines = ["import requests", "", "url = %s" % js(url), "headers = {%s}" % ", ".join("%s: %s" % (js(k), js(v)) for k, v in hdrs)]
        if body is not None and not binary:
            lines.append("payload = %s" % js(body))
        lines += ["", "response = requests.request(%s, url, headers=headers%s)" % (js(m), ", data=payload" if body is not None and not binary else ""),
                  "print(response.status_code)", "print(response.text)"]
        return "\n".join(lines)
    if lang == "javascript":
        opts = ["  method: %s," % js(m), "  headers: {%s}," % ", ".join("%s: %s" % (js(k), js(v)) for k, v in hdrs)]
        if body is not None and not binary:
            opts.append("  body: %s," % js(body))
        return "const response = await fetch(%s, {\n%s\n});\nconsole.log(response.status, await response.text());" % (js(url), "\n".join(opts))
    if lang == "java":
        pub = "HttpRequest.BodyPublishers.ofString(%s)" % js(body) if body is not None and not binary else "HttpRequest.BodyPublishers.noBody()"
        hl = "".join('\n        .header(%s, %s)' % (js(k), js(v)) for k, v in hdrs if k.lower() not in ("host", "content-length", "connection"))
        return ("import java.net.URI;\nimport java.net.http.HttpClient;\nimport java.net.http.HttpRequest;\nimport java.net.http.HttpResponse;\n\n"
                "HttpClient client = HttpClient.newHttpClient();\nHttpRequest request = HttpRequest.newBuilder()\n"
                "        .uri(URI.create(%s))%s\n        .method(%s, %s)\n        .build();\n"
                "HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());\n"
                "System.out.println(response.statusCode());\nSystem.out.println(response.body());") % (js(url), hl, js(m), pub)
    if lang == "csharp":
        ct = next((v for k, v in hdrs if k.lower() == "content-type"), "text/plain")
        hl = "".join('\nrequest.Headers.TryAddWithoutValidation(%s, %s);' % (js(k), js(v)) for k, v in hdrs if k.lower() != "content-type")
        content = ('\nrequest.Content = new StringContent(%s, System.Text.Encoding.UTF8, %s);' % (js(body), js(ct.split(";")[0]))) if body is not None and not binary else ""
        return ("using var client = new HttpClient();\nvar request = new HttpRequestMessage(new HttpMethod(%s), %s);%s%s\n"
                "var response = await client.SendAsync(request);\nConsole.WriteLine((int)response.StatusCode);\n"
                "Console.WriteLine(await response.Content.ReadAsStringAsync());") % (js(m), js(url), hl, content)
    if lang == "go":
        bodyexpr = "strings.NewReader(%s)" % js(body) if body is not None and not binary else "nil"
        hl = "".join("\n\treq.Header.Set(%s, %s)" % (js(k), js(v)) for k, v in hdrs)
        return ('package main\n\nimport (\n\t"fmt"\n\t"io"\n\t"net/http"\n\t"strings"\n)\n\nfunc main() {\n'
                '\treq, _ := http.NewRequest(%s, %s, %s)%s\n\tres, err := http.DefaultClient.Do(req)\n'
                '\tif err != nil {\n\t\tpanic(err)\n\t}\n\tdefer res.Body.Close()\n\tbody, _ := io.ReadAll(res.Body)\n'
                '\tfmt.Println(res.StatusCode, string(body))\n}') % (js(m), js(url), bodyexpr, hl)
    if lang == "powershell":
        hl = "; ".join("'%s' = '%s'" % (k.replace("'", "''"), v.replace("'", "''")) for k, v in hdrs)
        return ("$headers = @{ %s }\n$response = Invoke-WebRequest -Uri '%s' -Method %s -Headers $headers%s\n$response.StatusCode\n$response.Content"
                % (hl, url.replace("'", "''"), m, (" -Body '%s'" % body.replace("'", "''")) if body is not None and not binary else ""))
    raise ValueError("Unknown language %r." % lang)


# ── YAML (enough for OpenAPI files) ──────────────────────────────────────
def load_yaml(text):
    """A small YAML reader for block-style documents such as OpenAPI specs: mappings, lists, scalars,
    quoted strings, | and > blocks, and simple [a, b] / {a: b} flow values. Anchors and tags aren't supported."""
    lines = []
    for raw in (text or "").replace("\t", "  ").splitlines():
        s = raw.rstrip()
        if not s.strip() or s.strip().startswith("#") or s.strip() in ("---", "..."):
            lines.append(None if not s.strip().startswith(("|", ">")) else s)
            continue
        lines.append(s)

    def strip_comment(v):
        out, q = [], None
        for i, ch in enumerate(v):
            if q:
                if ch == q:
                    q = None
            elif ch in "'\"":
                q = ch
            elif ch == "#" and (i == 0 or v[i - 1] == " "):
                break
            out.append(ch)
        return "".join(out).strip()

    def scalar(v):
        v = strip_comment(v)
        if v == "":
            return None
        if v[0] in "'\"" and v[-1] == v[0]:
            return v[1:-1].replace("''", "'") if v[0] == "'" else json.loads(v) if "\\" in v else v[1:-1]
        if v[0] in "[{":
            return flow(v)
        low = v.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if low in ("null", "~"):
            return None
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        if re.fullmatch(r"-?\d+\.\d*(e[-+]?\d+)?", low):
            return float(v)
        return v

    def flow(v):
        try:
            return json.loads(v)
        except ValueError:
            pass
        inner = v[1:-1].strip()
        parts, depth, cur, q = [], 0, "", None
        for ch in inner:
            if q:
                q = None if ch == q else q
            elif ch in "'\"":
                q = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(cur.strip())
                cur = ""
                continue
            cur += ch
        if cur.strip():
            parts.append(cur.strip())
        if v[0] == "[":
            return [scalar(p) for p in parts]
        out = {}
        for p in parts:
            k, _, val = p.partition(":")
            out[str(scalar(k))] = scalar(val)
        return out

    pos = [0]
    n = len(lines)

    def indent(i):
        return len(lines[i]) - len(lines[i].lstrip())

    def skip():
        while pos[0] < n and lines[pos[0]] is None:
            pos[0] += 1

    def block_scalar(style, ind):
        out = []
        while pos[0] < n:
            ln = lines[pos[0]]
            if ln is not None and indent(pos[0]) <= ind:
                break
            out.append("" if ln is None else ln)
            pos[0] += 1
        base = min((len(x) - len(x.lstrip()) for x in out if x.strip()), default=0)
        body = [x[base:] for x in out]
        while body and not body[-1].strip():
            body.pop()
        return "\n".join(body) + "\n" if style.startswith("|") else " ".join(x.strip() for x in body) + "\n"

    def parse(ind):
        skip()
        if pos[0] >= n:
            return None
        if lines[pos[0]].lstrip().startswith("- ") or lines[pos[0]].strip() == "-":
            return parse_list(indent(pos[0]))
        return parse_map(indent(pos[0]))

    def value_after(rest, ind):
        rest = rest.strip()
        if rest.startswith(("|", ">")):
            pos[0] += 1
            return block_scalar(rest, ind)
        pos[0] += 1
        if strip_comment(rest) == "":
            skip()
            if pos[0] < n and (indent(pos[0]) > ind or (indent(pos[0]) == ind and lines[pos[0]].lstrip().startswith("- "))):
                return parse(indent(pos[0]))
            return None
        return scalar(rest)

    def parse_map(ind):
        out = {}
        while True:
            skip()
            if pos[0] >= n or indent(pos[0]) != ind or lines[pos[0]].lstrip().startswith("- "):
                return out
            s = lines[pos[0]].strip()
            m = re.match(r"""^("(?:[^"\\]|\\.)*"|'[^']*'|[^:#][^:]*?)\s*:(\s+|$)(.*)$""", s)
            if not m:
                raise ValueError("Couldn't read YAML line %d: %s" % (pos[0] + 1, s))
            key = scalar(m.group(1))
            out[str(key)] = value_after(m.group(3), ind)

    def parse_list(ind):
        out = []
        while True:
            skip()
            if pos[0] >= n or indent(pos[0]) != ind or not (lines[pos[0]].lstrip().startswith("- ") or lines[pos[0]].strip() == "-"):
                return out
            s = lines[pos[0]].strip()[1:].strip()
            if not s:
                pos[0] += 1
                skip()
                out.append(parse(indent(pos[0])) if pos[0] < n and indent(pos[0]) > ind else None)
            elif re.match(r"""^("(?:[^"\\]|\\.)*"|'[^']*'|[^:#\[{][^:]*?)\s*:(\s+|$)""", s):
                # "- key: value" starts a mapping indented past the dash
                lines[pos[0]] = " " * (ind + 2) + s
                out.append(parse_map(ind + 2))
            else:
                out.append(value_after(s, ind) if s.startswith(("|", ">")) else scalar(s))
                if not s.startswith(("|", ">")):
                    pos[0] += 1
        return out

    try:
        result = parse(0)
    except (IndexError, ValueError) as e:
        raise ValueError(str(e))
    if not isinstance(result, (dict, list)):
        raise ValueError("That isn't YAML.")
    return result


# ── OpenAPI ──────────────────────────────────────────────────────────────
def _ref(spec, node, depth=0):
    while isinstance(node, dict) and "$ref" in node and depth < 20:
        path = node["$ref"].lstrip("#/").split("/")
        cur = spec
        for p in path:
            cur = (cur or {}).get(p.replace("~1", "/").replace("~0", "~")) if isinstance(cur, dict) else None
        node, depth = cur or {}, depth + 1
    return node


def example_of(spec, schema, depth=0):
    """An example value for a JSON schema (uses example/default/enum, else a typed placeholder)."""
    schema = _ref(spec, schema or {})
    if depth > 6 or not isinstance(schema, dict):
        return None
    for k in ("example", "default"):
        if k in schema:
            return schema[k]
    if schema.get("examples") and isinstance(schema["examples"], list):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    for k in ("allOf",):
        if schema.get(k):
            out = {}
            for s in schema[k]:
                v = example_of(spec, s, depth + 1)
                if isinstance(v, dict):
                    out.update(v)
            return out
    for k in ("oneOf", "anyOf"):
        if schema.get(k):
            return example_of(spec, schema[k][0], depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or "properties" in schema:
        return {k: example_of(spec, v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [example_of(spec, schema.get("items") or {}, depth + 1)]
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return True
    fmt = schema.get("format", "")
    return {"date-time": "2024-01-01T00:00:00Z", "date": "2024-01-01", "email": "user@example.com", "uuid": str(uuid.UUID(int=0)),
            "uri": "https://example.com"}.get(fmt, "string")


def inline_schema(spec, schema, depth=0):
    """A schema with its $refs replaced by what they point to (so a check can carry it)."""
    schema = _ref(spec, schema)
    if depth > 12 or not isinstance(schema, (dict, list)):
        return schema
    if isinstance(schema, list):
        return [inline_schema(spec, s, depth + 1) for s in schema]
    return {k: inline_schema(spec, v, depth + 1) if isinstance(v, (dict, list)) else v for k, v in schema.items()}


def from_openapi(spec):
    """A collection (folders by tag) and an environment with baseUrl, from an OpenAPI 3.x or Swagger 2 spec.
    Each request gets an example body, its parameters, and checks for the documented status and response schema."""
    if not isinstance(spec, dict):
        raise ValueError("That isn't an OpenAPI document.")
    info = spec.get("info") or {}
    col = blank_collection(info.get("title") or "API")
    col["description"] = info.get("description") or ""
    if "openapi" in spec:
        servers = spec.get("servers") or [{}]
        base = (servers[0] or {}).get("url") or "http://localhost:3000"
        for name, var in ((servers[0] or {}).get("variables") or {}).items():
            base = base.replace("{%s}" % name, str((var or {}).get("default", "")))
    else:
        scheme = (spec.get("schemes") or ["https"])[0]
        base = "%s://%s%s" % (scheme, spec.get("host") or "localhost", spec.get("basePath") or "")
    base = base.rstrip("/")
    env = {"id": new_id(), "name": (info.get("title") or "API") + " (from spec)", "vars": [{"key": "baseUrl", "value": base, "secret": False, "enabled": True}]}
    col["variables"] = [{"key": "baseUrl", "value": base, "enabled": True}]     # like Postman: works before an environment is picked
    schemes = (spec.get("components") or {}).get("securitySchemes") or spec.get("securityDefinitions") or {}
    sec = spec.get("security") or []
    if sec and isinstance(sec[0], dict) and sec[0]:
        sch = _ref(spec, schemes.get(next(iter(sec[0])), {}))
        t, s = sch.get("type"), (sch.get("scheme") or "").lower()
        if t == "http" and s == "bearer":
            col["auth"] = {"type": "bearer", "token": "{{token}}"}
            env["vars"].append({"key": "token", "value": "", "secret": True, "enabled": True})
        elif t == "http" and s == "basic" or t == "basic":
            col["auth"] = {"type": "basic", "username": "{{username}}", "password": "{{password}}"}
        elif t == "apiKey":
            col["auth"] = {"type": "apikey", "key": sch.get("name", "X-API-Key"), "value": "{{apiKey}}", "in": "query" if sch.get("in") == "query" else "header"}
            env["vars"].append({"key": "apiKey", "value": "", "secret": True, "enabled": True})
        elif t == "oauth2":
            flows = sch.get("flows") or {}
            flow = flows.get("clientCredentials") or flows.get("authorizationCode") or flows.get("password") or {}
            grant = "client_credentials" if "clientCredentials" in flows else "authorization_code" if "authorizationCode" in flows else "password"
            col["auth"] = {"type": "oauth2", "grant": grant, "token_url": flow.get("tokenUrl", ""), "auth_url": flow.get("authorizationUrl", ""),
                           "client_id": "{{clientId}}", "client_secret": "{{clientSecret}}", "scope": " ".join((flow.get("scopes") or {}).keys()), "pkce": True}
    folders = {}
    for path, ops in (spec.get("paths") or {}).items():
        ops = _ref(spec, ops)
        shared = ops.get("parameters") or []
        for meth, op in ops.items():
            if meth.upper() not in METHODS or not isinstance(op, dict):
                continue
            name = op.get("summary") or op.get("operationId") or "%s %s" % (meth.upper(), path)
            r = blank_request(name, meth.upper(), "{{baseUrl}}" + re.sub(r"\{(\w+)\}", r":\1", path))
            r["description"] = op.get("description") or ""
            if op.get("security") == []:
                r["auth"] = {"type": "none"}        # an open endpoint in a secured API
            for prm in shared + (op.get("parameters") or []):
                prm = _ref(spec, prm)
                ex = prm.get("example", example_of(spec, prm.get("schema") or {k: prm[k] for k in ("type", "enum", "default", "format") if k in prm}))
                ex = "" if ex is None else str(ex).lower() if isinstance(ex, bool) else str(ex)
                row = {"key": prm.get("name", ""), "value": ex, "enabled": bool(prm.get("required"))}
                where = prm.get("in")
                if where == "path":
                    r["path_vars"] = r.get("path_vars", []) + [dict(row, enabled=True)]
                elif where == "query":
                    r["params"].append(row)
                elif where == "header":
                    r["headers"].append(row)
                elif where == "body":            # Swagger 2
                    r["body"] = {"mode": "json", "text": json.dumps(example_of(spec, prm.get("schema")), indent=2), "form": []}
            rb = _ref(spec, op.get("requestBody") or {})
            content = rb.get("content") or {}
            if content:
                ctype = next((c for c in content if "json" in c), next(iter(content)))
                media = content[ctype] or {}
                ex = media.get("example")
                if ex is None and isinstance(media.get("examples"), dict) and media["examples"]:
                    ex = _ref(spec, next(iter(media["examples"].values()))).get("value")
                if ex is None:
                    ex = example_of(spec, media.get("schema"))
                if "json" in ctype:
                    r["body"] = {"mode": "json", "text": json.dumps(ex, indent=2), "form": []}
                elif "x-www-form-urlencoded" in ctype and isinstance(ex, dict):
                    r["body"] = {"mode": "form", "text": "", "form": [{"key": k, "value": str(v), "enabled": True} for k, v in ex.items()]}
                elif "multipart" in ctype and isinstance(ex, dict):
                    r["body"] = {"mode": "multipart", "text": "", "form": [{"key": k, "value": str(v), "enabled": True, "type": "text"} for k, v in ex.items()]}
                else:
                    r["body"] = {"mode": "text", "text": ex if isinstance(ex, str) else json.dumps(ex), "form": [], "content_type": ctype}
            responses = op.get("responses") or {}
            ok = next((c for c in responses if str(c).startswith("2")), None)
            r["checks"] = [{"kind": "status", "op": "eq", "value": str(ok) if ok else "2xx", "enabled": True}]
            if ok:
                resp = _ref(spec, responses[ok])
                schema = None
                if "content" in resp:
                    jc = next((c for c in resp["content"] if "json" in c), None)
                    schema = (resp["content"][jc] or {}).get("schema") if jc else None
                elif "schema" in resp:
                    schema = resp["schema"]
                if schema:
                    r["checks"].append({"kind": "schema", "op": "valid", "schema": inline_schema(spec, schema), "enabled": True})
                    r["examples"] = [{"name": "Example %s" % ok, "status": int(ok) if str(ok).isdigit() else 200, "reason": resp.get("description", ""),
                                      "headers": [["Content-Type", "application/json"]], "body": json.dumps(example_of(spec, schema), indent=2)}]
            tag = (op.get("tags") or ["Other"])[0]
            if tag not in folders:
                folders[tag] = {"id": new_id(), "name": tag, "folder": True, "items": [], "auth": {"type": "inherit"},
                                "pre_script": "", "test_script": ""}
            folders[tag]["items"].append(r)
    col["items"] = list(folders.values()) if len(folders) > 1 else (list(folders.values())[0]["items"] if folders else [])
    return col, env


# ── JSON schema (the parts APIs use) ─────────────────────────────────────
def validate_schema(value, schema, path="$", errors=None, depth=0):
    """Problems with value against a JSON schema: type, required, properties, items, enum, const,
    min/max (length, items, value), pattern, nullable, allOf/anyOf/oneOf, additionalProperties: false."""
    errors = [] if errors is None else errors
    if depth > 40 or not isinstance(schema, dict) or len(errors) > 20:
        return errors
    if value is None and (schema.get("nullable") or "null" in (schema.get("type") if isinstance(schema.get("type"), list) else [])):
        return errors
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    actual = _jtype(value)
    if types:
        okt = any(actual == x or (x == "integer" and isinstance(value, int) and not isinstance(value, bool)) or
                  (x == "number" and actual == "number") for x in types)
        if not okt:
            errors.append("%s should be %s, but is %s" % (path, " or ".join(types), actual))
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s should be one of %s" % (path, json.dumps(schema["enum"])[:120]))
    if "const" in schema and value != schema["const"]:
        errors.append("%s should be %s" % (path, json.dumps(schema["const"])))
    for sub in schema.get("allOf") or []:
        validate_schema(value, sub, path, errors, depth + 1)
    for k in ("anyOf", "oneOf"):
        if schema.get(k):
            good = sum(1 for sub in schema[k] if not validate_schema(value, sub, path, [], depth + 1))
            if good == 0 or (k == "oneOf" and good > 1):
                errors.append("%s doesn't match %s the allowed shapes" % (path, "any of" if good == 0 else "exactly one of"))
    if isinstance(value, dict):
        for req in schema.get("required") or []:
            if req not in value:
                errors.append("%s is missing %r" % (path, req))
        props = schema.get("properties") or {}
        for k, v in value.items():
            if k in props:
                validate_schema(v, props[k], "%s.%s" % (path, k), errors, depth + 1)
            elif schema.get("additionalProperties") is False:
                errors.append("%s has an unexpected field %r" % (path, k))
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(v, schema["additionalProperties"], "%s.%s" % (path, k), errors, depth + 1)
    if isinstance(value, list):
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(value[:200]):
                validate_schema(v, schema["items"], "%s[%d]" % (path, i), errors, depth + 1)
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s should have at least %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("%s should have at most %d items" % (path, schema["maxItems"]))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s is shorter than %d characters" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s is longer than %d characters" % (path, schema["maxLength"]))
        if schema.get("pattern"):
            try:
                if not re.search(schema["pattern"], value):
                    errors.append("%s doesn't match %s" % (path, schema["pattern"]))
            except re.error:
                pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s is below %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s is above %s" % (path, schema["maximum"]))
    return errors
