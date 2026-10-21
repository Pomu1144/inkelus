"""The command center: a small local web server that runs the whole app.

    python run.py              (or double-click run.pyw on Windows)
    python -m vscode_assistant [--port 8765] [--no-open] [--tab] [--keep-running]

It listens on 127.0.0.1 only and opens the app in your browser (Chrome if it
can find it). It opens further windows of its own when needed (the agent
browser). Standard library only, like the rest of the app.

Every API call must carry the random token that is baked into the page, and
the Host header must be localhost, so other web pages can't drive it.
"""
import base64
import csv
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from . import agent_browser, api, apirun, engine, gitops, javadebug, javals, net, projects, providers, realtime, tasks, web
from .library import KINDS, Library
from .store import Store, new_id

mimetypes.add_type("image/webp", ".webp")   # older Pythons don't know it
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
APP_NAME = "VSCode Assistant"
MAX_BODY = 300 * 1024 * 1024    # big enough for a zipped project
KEY_MASK = "••••••••"
IDLE_EXIT = 30   # seconds with no open app window before the server stops


class Hub:
    """App state shared by all request threads, plus the event fan-out."""

    def __init__(self):
        self.store = Store()
        self.library = Library(self.store.dir)
        self.projects = projects.Projects(self.store)
        self.projects.tests.publish = self.publish
        self.projects.library = self.library
        self.api = api.Store(self.store.dir)
        self.servers = api.Servers(self.publish)
        self.projects.api = self.api
        self.projects.servers = self.servers
        self.projects.script_runner = self.run_script
        self.script_calls = {}       # script run id -> {"event", "result"} (scripts run in the app window's sandbox)
        self.api_reports = {}        # collection run reports, for downloading
        self.rt = realtime.Sessions(self.publish)   # open WebSocket / SSE connections
        self.java = javals.Java(self.store.dir, self.store.settings.setdefault("java", {}), self.publish)   # Java language servers
        self.debug = {}             # project id -> javadebug.Session
        self.oauth_pending = {}      # OAuth state -> {"auth", "vals", "verifier", "redirect"}
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.clients = []           # one queue per open event stream
        self.streams = {}           # chat id -> cancel Event
        self.sandboxes = {}         # chat id -> web.Sandbox
        self.confirms = {}          # confirm id -> {"event", "ok", "all", "chat"}
        self.pulls = {}             # "conn::model" -> cancel Event
        self.cred_requests = {}     # request id -> {"event", "answer", "chat", "host", "url", "fields"}
        self._last_push = {}
        self.seen_client = False
        self.last_client = time.time()
        engine.agent_for(self.store).credential_provider = self.request_credential

    # API scripts run in the app window's sandboxed worker (or Node.js when no window is open)
    def run_script(self, ctx):
        with self.lock:
            has_window = bool(self.clients)
        if has_window:
            rid = secrets.token_hex(8)
            box = self.script_calls[rid] = {"event": threading.Event(), "result": None}
            self.publish({"type": "script", "id": rid, "ctx": ctx})
            try:
                if box["event"].wait(12) and isinstance(box["result"], dict):
                    return box["result"]
            finally:
                self.script_calls.pop(rid, None)
        return apirun.node_runner(ctx)

    # OAuth 2.0 authorization code: the user signs in in a browser window, which comes back to /oauth/callback
    def oauth_begin(self, auth, vals, redirect):
        import hashlib
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        f = lambda k: api.fill(auth.get(k), vals)
        if not f("auth_url"):
            raise ValueError("OAuth 2.0: set the authorization URL.")
        q = {"response_type": "code", "client_id": f("client_id"), "redirect_uri": redirect, "state": state}
        if f("scope"):
            q["scope"] = f("scope")
        if f("audience"):
            q["audience"] = f("audience")
        if auth.get("pkce", True):
            q["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            q["code_challenge_method"] = "S256"
        self.oauth_pending[state] = {"auth": auth, "vals": vals, "verifier": verifier, "redirect": redirect, "when": time.time()}
        return f("auth_url") + ("&" if "?" in f("auth_url") else "?") + urllib.parse.urlencode(q)

    def oauth_finish(self, state, code, error=""):
        p = self.oauth_pending.pop(state, None)
        if not p:
            return False, "This sign-in link has expired. Press Get new token again."
        if error or not code:
            return False, "The sign-in was cancelled or refused: %s" % (error or "no code")
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect"]}
        if p["auth"].get("pkce", True):
            form["code_verifier"] = p["verifier"]
        try:
            api.store_token(self.api, p["auth"], p["vals"], api.token_request(p["auth"], p["vals"], form))
        except ValueError as e:
            self.publish({"type": "oauth", "ok": False, "error": str(e)})
            return False, str(e)
        self.publish({"type": "oauth", "ok": True})
        return True, "Signed in. You can close this window and go back to the app."

    # saved logins
    def request_credential(self, chat_id, host, url, fields):
        """Ask the open app window for a saved login for `host`. Blocks until the user answers
        (unlocking the vault and approving, or declining). The secret is used once and not kept."""
        rid = secrets.token_hex(8)
        ev = threading.Event()
        req = self.cred_requests[rid] = {"event": ev, "answer": None, "chat": chat_id, "host": host,
                                         "url": url, "fields": fields}
        self.publish({"type": "credential_request", "id": rid, "chat_id": chat_id, "host": host, "url": url,
                      "fields": fields})
        try:
            end = time.time() + 600
            while not ev.wait(0.25):
                cancel = self.streams.get(chat_id)
                if cancel is None or cancel.is_set() or time.time() > end:
                    return None
            return req["answer"]
        finally:
            self.cred_requests.pop(rid, None)
            self.publish({"type": "credential_done", "id": rid})

    # events
    def publish(self, ev):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            q.put(ev)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
            self.seen_client = True
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)
            self.last_client = time.time()

    # storage
    def save_chats(self):
        with self.lock:
            self.store.save_chats()

    def save_settings(self):
        with self.lock:
            self.store.save_settings()

    def chat(self, cid):
        return next((c for c in self.store.chats if c["id"] == cid), None)

    # generation
    def start(self, c):
        cid, model = engine.split_model(c.get("model"))
        conn = self.store.conn(cid)
        if not conn:
            raise ValueError("That model's server was removed. Pick another model in Settings.")
        if c["id"] in self.streams:
            raise ValueError("This chat is already generating.")
        cancel = threading.Event()
        self.streams[c["id"]] = cancel
        c["updated"] = time.time()
        box = self.sandboxes.setdefault(c["id"], web.Sandbox()) if c.get("browser") else None
        chat_id = c["id"]

        def emit(kind, *args):
            if kind == "update":
                self.publish({"type": "chat", "chat": chat_summary(c, self), "messages": c["messages"]})
            elif kind == "delta":
                now = time.time()
                if now - self._last_push.get(chat_id, 0) >= 0.05:
                    self._last_push[chat_id] = now
                    self.publish({"type": "delta", "chat_id": chat_id, "index": len(c["messages"]) - 1,
                                  "message": c["messages"][-1]})
            elif kind == "done":
                self.streams.pop(chat_id, None)
                c["updated"] = time.time()
                c.pop("allow_all_tools", None)
                self.save_chats()
                self.publish({"type": "chat", "chat": chat_summary(c, self), "messages": c["messages"]})
                self.publish({"type": "done", "chat_id": chat_id, "info": args[0]})
            elif kind == "toast":
                self.publish({"type": "toast", "text": args[0], "bad": True})

        def ask_tool(chat, call, cancel_ev, preview=None):
            rid = new_id()
            ev = threading.Event()
            self.confirms[rid] = {"event": ev, "ok": False, "all": False, "chat": chat["id"], "call": call,
                                  "preview": preview, "project": chat.get("project") or ""}
            self.publish({"type": "confirm", "id": rid, "chat_id": chat["id"], "call": call, "preview": preview})
            try:
                while not ev.wait(0.2):
                    if cancel_ev.is_set():
                        raise net.Cancelled()
                box_ = self.confirms[rid]
                if box_["all"]:
                    chat["allow_all_tools"] = True
                return box_["ok"]
            finally:
                self.confirms.pop(rid, None)
                self.publish({"type": "confirm_done", "id": rid})

        agent = engine.agent_for(self.store) if c.get("agent") else None
        threading.Thread(target=engine.run_turn, daemon=True,
                         args=(self.store, self.library, c, conn, model, cancel, emit, ask_tool, box, agent,
                               self.projects)).start()
        self.publish({"type": "chat", "chat": chat_summary(c, self), "messages": c["messages"]})


def chat_summary(c, hub):
    return {"id": c["id"], "title": c["title"], "updated": c.get("updated", 0), "model": c.get("model", ""),
            "busy": c["id"] in hub.streams, "group": c.get("group") or "", "project": c.get("project") or ""}


def public_settings(s):
    out = json.loads(json.dumps(s))
    for c in out["connections"]:
        c["key"] = KEY_MASK if c.get("key") else ""
    return out


def model_list(hub):
    """Every model on every server, as picker entries."""
    values, offline = [], []
    for cn in hub.store.settings["connections"]:
        try:
            for m in providers.create(cn).list_models():
                extra = " · " + m["params"] if m.get("params") else ""
                values.append({"id": cn["id"] + "::" + m["name"], "label": "%s%s   (%s)" % (m["name"], extra, cn["name"])})
        except Exception as e:
            offline.append({"conn": cn["name"], "error": str(e)})
    return {"models": values, "offline": offline}


class Handler(BaseHTTPRequestHandler):
    hub = None  # set by serve()
    server_version = "VSCodeAssistant"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet console
        pass

    # ── plumbing ────────────────────────────────────────────────────────
    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def ok(self, data=None):
        self.send(200, data if data is not None else {"ok": True})

    def fail(self, code, msg):
        self.send(code, {"error": msg})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Request too large.")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw.strip() else {}

    def host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def authed(self, query):
        tok = self.headers.get("X-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(tok, self.hub.token)

    def route(self, method):
        if not self.host_ok():
            return self.fail(403, "Use http://127.0.0.1 to open the app.")
        u = urlsplit(self.path)
        path, query = u.path, parse_qs(u.query)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.index()
            if method == "GET" and path.startswith("/static/"):
                return self.static(STATIC, path[len("/static/"):])
            if method == "GET" and path.startswith("/assets/"):
                return self.static(ASSETS, path[len("/assets/"):])
            if method == "GET" and path == "/oauth/callback":
                ok, msg = self.hub.oauth_finish((query.get("state") or [""])[0], (query.get("code") or [""])[0],
                                                (query.get("error_description") or query.get("error") or [""])[0])
                return self.send(200 if ok else 400, "<!doctype html><meta charset=utf-8><title>Sign-in</title>"
                                 "<body style='font:15px system-ui;padding:40px;background:#262624;color:#f5f4ef'><p>%s</p>"
                                 "<script>setTimeout(()=>window.close(),1500)</script>" % html.escape(msg), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self.fail(404, "Not found")
            if not self.authed(query):
                return self.fail(401, "Reload the page.")
            parts = [p for p in path[len("/api/"):].split("/") if p]
            handler = getattr(self, "api_" + (parts[0] if parts else ""), None)
            if not handler:
                return self.fail(404, "Unknown API")
            return handler(method, parts[1:], query)
        except (ValueError, KeyError, TypeError) as e:
            return self.fail(400, str(e))
        except (ConnectionError, net.HTTPError, RuntimeError, OSError) as e:
            return self.fail(502, str(e))

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def index(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read().replace("__TOKEN__", self.hub.token).replace("__APP_NAME__", APP_NAME)
        self.send(200, html, "text/html; charset=utf-8",
                  {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                              "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                              "frame-ancestors 'none'"})

    def static(self, root, rel):
        p = os.path.normpath(os.path.join(root, rel))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return self.fail(404, "Not found")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        headers = None
        if os.path.basename(p) == "pmsandbox.js":
            # API scripts run in a worker made from this file: they may run code, but not load or fetch anything
            headers = {"Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-eval'"}
        with open(p, "rb") as f:
            self.send(200, f.read(), ctype, headers)

    # ── API ─────────────────────────────────────────────────────────────
    def api_events(self, method, parts, query):
        """Server-sent events: streaming replies, tool confirmations, toasts."""
        q = self.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True
        # replay pending confirmations so a reloaded page can still answer them
        pending = [{"type": "confirm", "id": rid, "chat_id": v["chat"], "call": v.get("call"), "preview": v.get("preview")}
                   for rid, v in list(self.hub.confirms.items()) if v.get("call")]
        pending += [{"type": "credential_request", "id": rid, "chat_id": v["chat"], "host": v["host"],
                     "url": v["url"], "fields": v["fields"]} for rid, v in list(self.hub.cred_requests.items())]
        try:
            for ev in pending:
                q.put(ev)
            while True:
                try:
                    ev = q.get(timeout=5)
                    data = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    data = ": ping\n\n"
                self.wfile.write(data.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def api_state(self, method, parts, query):
        h = self.hub
        chats = sorted(h.store.chats, key=lambda c: -c.get("updated", 0))
        self.ok({"app": APP_NAME, "settings": public_settings(h.store.settings),
                 "chats": [chat_summary(c, h) for c in chats],
                 "library": {k: h.library.names(k) for k in KINDS},
                 "projects": [h.projects.summary(p) for p in h.projects.all()], "git": bool(gitops.git_path()),
                 "data_dir": h.store.dir, "tools": web.TOOL_NAMES})

    def api_search(self, method, parts, query):
        q = (query.get("q") or [""])[0].strip().lower()
        hits = [c["id"] for c in self.hub.store.chats if q in c["title"].lower()
                or any(q in (m.get("content") or "").lower() for m in c["messages"])]
        self.ok({"ids": hits})

    def api_chats(self, method, parts, query):
        h = self.hub
        if not parts:
            if method == "POST":
                b = self.body()
                c = h.store.new_chat(b.get("model") or "")
                h.save_chats()
                return self.ok({"chat": c})
            if method == "DELETE":  # delete all
                for cancel in list(h.streams.values()):
                    cancel.set()
                h.store.chats.clear()
                h.save_chats()
                return self.ok()
            return self.fail(405, "Method not allowed")
        c = h.chat(parts[0])
        if not c:
            return self.fail(404, "That conversation no longer exists.")
        sub = parts[1] if len(parts) > 1 else ""
        if not sub:
            if method == "GET":
                return self.ok({"chat": c, "busy": c["id"] in h.streams})
            if method == "PATCH":
                b = self.body()
                for k in ("title", "model", "browser", "agent", "docs", "template", "group", "project"):
                    if k in b:
                        c[k] = b[k]
                if "title" in b:
                    c["titled"] = True
                if "params" in b and isinstance(b["params"], dict):
                    c["params"].update(b["params"])
                if b.get("model"):
                    h.store.settings["last_model"] = b["model"]
                    h.save_settings()
                h.save_chats()
                return self.ok({"chat": c})
            if method == "DELETE":
                if c["id"] in h.streams:
                    h.streams[c["id"]].set()
                engine.agent_for(h.store).close_chat(c["id"])
                h.store.chats.remove(c)
                h.save_chats()
                return self.ok()
        if sub == "send" and method == "POST":
            b = self.body()
            text = (b.get("text") or "").strip()
            images = [i.split(",", 1)[-1] for i in b.get("images") or []]
            model = b.get("model") or c.get("model") or h.store.settings.get("last_model")
            if not model or not engine.split_model(model)[0]:
                return self.fail(400, "No model selected. Pick one in Settings › Model.")
            if not text and not images:
                return self.fail(400, "Nothing to send.")
            if c["id"] in h.streams:
                return self.fail(409, "This chat is already generating.")
            c["model"] = model
            h.store.settings["last_model"] = model
            h.save_settings()
            c["messages"].append({"role": "user", "content": text, "images": images})
            if not c.get("titled"):
                t = " ".join(text.split()) or "Image"
                c["title"] = t if len(t) <= 48 else t[:46] + "…"
            h.start(c)
            return self.ok()
        if sub == "stop" and method == "POST":
            if c["id"] in h.streams:
                h.streams[c["id"]].set()
            return self.ok()
        if sub == "action" and method == "POST":
            if c["id"] in h.streams:
                return self.fail(409, "Wait for the reply to finish.")
            b = self.body()
            act, i = b.get("act"), int(b.get("index", -1))
            if not 0 <= i < len(c["messages"]):
                return self.fail(400, "No such message.")
            if act == "delete":
                del c["messages"][i]
            elif act == "truncate":  # edit: drop this message and everything after it
                del c["messages"][i:]
            elif act == "regen":
                j = i
                while j > 0 and c["messages"][j - 1]["role"] != "user":
                    j -= 1
                del c["messages"][j:]
                if b.get("model"):
                    c["model"] = b["model"]
                h.save_chats()
                h.start(c)
                return self.ok({"chat": c})
            else:
                return self.fail(400, "Unknown action.")
            h.save_chats()
            return self.ok({"chat": c})
        if sub == "export" and method == "GET":
            safe = "".join(ch for ch in c["title"] if ch.isalnum() or ch in " -_").strip()[:60] or "chat"
            return self.send(200, engine.chat_markdown(c), "text/markdown; charset=utf-8",
                             {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe + ".md")})
        return self.fail(404, "Unknown chat action")

    def api_confirm(self, method, parts, query):
        b = self.body()
        box = self.hub.confirms.get(b.get("id"))
        if not box:
            return self.ok({"stale": True})
        box["ok"], box["all"] = bool(b.get("ok")), bool(b.get("all"))
        if box["call"].get("name") in projects.ALWAYS_ASK or box["call"].get("name") == "run_command":
            box["all"] = False            # pushes and commands are never allowed wholesale
        if b.get("ok") and b.get("allow_prefix") and box.get("project"):
            self.hub.projects.allow_command(box["project"], str(b["allow_prefix"]))
        note = str(b.get("note") or "").strip()
        if not b.get("ok") and note:
            c = self.hub.chat(box["chat"])
            if c is not None:
                c["decline_note"] = note[:2000]
        box["event"].set()
        return self.ok()

    def api_models(self, method, parts, query):
        h = self.hub
        if not parts:
            return self.ok(model_list(h))
        conn = h.store.conn(parts[0])
        if not conn:
            return self.fail(404, "Unknown server.")
        prov = providers.create(conn)
        act = parts[1] if len(parts) > 1 else "list"
        if act == "list":
            out = {"models": prov.list_models(), "running": [], "caps": prov.caps}
            if prov.caps.get("running"):
                try:
                    out["running"] = prov.running()
                except Exception:
                    pass
            return self.ok(out)
        if act == "ping":
            return self.ok({"text": prov.ping()})
        b = self.body()
        name = b.get("name", "")
        if act == "show":
            return self.ok({"info": prov.show(name)})
        if act == "delete":
            prov.remove(name)
            return self.ok()
        if act == "load":
            prov.load(name, h.store.settings["defaults"].get("keep_alive") or None)
            return self.ok()
        if act == "unload":
            prov.unload(name)
            return self.ok()
        if act == "pull":
            key = conn["id"] + "::" + name
            if key in h.pulls:
                return self.fail(409, "Already pulling %s." % name)
            cancel = h.pulls[key] = threading.Event()

            def run():
                last = [0.0]

                def progress(j):
                    now = time.time()
                    if now - last[0] > 0.2 or j.get("status") == "success":
                        last[0] = now
                        h.publish({"type": "pull", "conn": conn["id"], "name": name, "progress": j})
                try:
                    prov.pull(name, progress, cancel)
                    h.publish({"type": "pull_done", "conn": conn["id"], "name": name, "ok": True})
                except net.Cancelled:
                    h.publish({"type": "pull_done", "conn": conn["id"], "name": name, "ok": False,
                               "error": "Cancelled"})
                except Exception as e:
                    h.publish({"type": "pull_done", "conn": conn["id"], "name": name, "ok": False, "error": str(e)})
                finally:
                    h.pulls.pop(key, None)

            threading.Thread(target=run, daemon=True).start()
            return self.ok()
        if act == "cancel_pull":
            ev = h.pulls.get(conn["id"] + "::" + name)
            if ev:
                ev.set()
            return self.ok()
        return self.fail(404, "Unknown model action")

    def api_settings(self, method, parts, query):
        h = self.hub
        s = h.store.settings
        if method == "GET":
            return self.ok({"settings": public_settings(s)})
        if method != "PATCH":
            return self.fail(405, "Method not allowed")
        b = self.body()
        if isinstance(b.get("web"), dict):
            for k in ("confirm", "allow_search", "allow_private"):
                if k in b["web"]:
                    s["web"][k] = bool(b["web"][k])
            if "max_steps" in b["web"]:
                s["web"]["max_steps"] = max(1, min(int(b["web"]["max_steps"]), 30))
        if isinstance(b.get("agent"), dict):
            a = s.setdefault("agent", {})
            if b["agent"].get("confirm") in ("actions", "all", "none"):
                a["confirm"] = b["agent"]["confirm"]
            if "chrome_path" in b["agent"]:
                a["chrome_path"] = str(b["agent"]["chrome_path"]).strip().strip('"')
            if "max_steps" in b["agent"]:
                a["max_steps"] = max(5, min(int(b["agent"]["max_steps"]), 60))
        if isinstance(b.get("java"), dict):
            j = s.setdefault("java", {})
            if "java_home" in b["java"]:
                j["java_home"] = str(b["java"]["java_home"]).strip().strip('"')
                h.java.tools.javas(refresh=True)
            if b["java"].get("memory") in ("1G", "2G", "4G"):
                j["memory"] = b["java"]["memory"]
        if isinstance(b.get("defaults"), dict):
            for k in ("system", "keep_alive"):
                if k in b["defaults"]:
                    s["defaults"][k] = str(b["defaults"][k])
        if isinstance(b.get("groups"), list):
            groups = []
            for g in b["groups"][:100]:
                if isinstance(g, dict) and str(g.get("id") or "").strip() and str(g.get("name") or "").strip():
                    groups.append({"id": str(g["id"])[:40], "name": str(g["name"]).strip()[:60],
                                   "collapsed": bool(g.get("collapsed"))})
            s["groups"] = groups
            known = {g["id"] for g in groups}
            moved = False
            for c in h.store.chats:   # chats in a deleted group go back to General
                if c.get("group") and c["group"] not in known:
                    c["group"] = ""
                    moved = True
            if moved:
                h.save_chats()
        for k in ("last_model", "default_conn", "web_theme"):
            if k in b:
                s[k] = b[k]
        h.save_settings()
        return self.ok({"settings": public_settings(s)})

    def api_connections(self, method, parts, query):
        h = self.hub
        s = h.store.settings
        if method == "POST":  # add or update
            b = self.body()
            data = {"name": (b.get("name") or "").strip(), "url": (b.get("url") or "").strip().rstrip("/"),
                    "type": "openai" if b.get("type") == "openai" else "ollama"}
            if not data["name"]:
                return self.fail(400, "Give the server a name.")
            if not data["url"].startswith(("http://", "https://")):
                return self.fail(400, "Base URL must start with http:// or https://")
            key = b.get("key") or ""
            existing = h.store.conn(b.get("id")) if b.get("id") else None
            if existing:
                if key != KEY_MASK:
                    data["key"] = key.strip()
                existing.update(data)
            else:
                data.update(id=new_id(), key=key.strip() if key != KEY_MASK else "")
                s["connections"].append(data)
                if not h.store.conn(s.get("default_conn")):
                    s["default_conn"] = data["id"]
            h.save_settings()
            return self.ok({"settings": public_settings(s)})
        if method == "DELETE" and parts:
            s["connections"] = [c for c in s["connections"] if c["id"] != parts[0]]
            if s.get("default_conn") == parts[0]:
                s["default_conn"] = s["connections"][0]["id"] if s["connections"] else None
            h.save_settings()
            return self.ok({"settings": public_settings(s)})
        return self.fail(405, "Method not allowed")

    def api_vault(self, method, parts, query):
        """The encrypted password vault. Encryption happens in the page (Web Crypto, AES-256-GCM with a
        PBKDF2 key from the user's master password); this side only stores the encrypted blob and
        relays one approved login at a time to the agent browser."""
        path = os.path.join(self.hub.store.dir, "vault.json")
        if parts == ["provide"] and method == "POST":
            b = self.body()
            req = self.hub.cred_requests.get(b.get("id"))
            if not req:
                return self.ok({"stale": True})
            if b.get("ok"):
                site = str(b.get("site") or "").strip().lower()
                req["answer"] = {"site": site, "username": str(b.get("username") or ""),
                                 "password": str(b.get("password") or "")}
            req["event"].set()
            return self.ok()
        if parts:
            return self.fail(404, "Unknown vault action")
        if method == "GET":
            try:
                with open(path, encoding="utf-8") as f:
                    return self.ok({"exists": True, "vault": json.load(f)})
            except (OSError, ValueError):
                return self.ok({"exists": False})
        if method == "PUT":
            v = self.body().get("vault")
            ok = isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("salt", "iv", "data")) \
                and isinstance(v.get("iterations"), int) and v["iterations"] >= 100000
            if not ok:
                return self.fail(400, "That isn't an encrypted vault.")
            v = {"format": "vscode-assistant-vault-1", "cipher": "AES-256-GCM", "kdf": "PBKDF2-SHA256",
                 "iterations": v["iterations"], "salt": v["salt"], "iv": v["iv"], "data": v["data"]}
            fd, tmp = tempfile.mkstemp(dir=self.hub.store.dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(v, f)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return self.ok()
        if method == "DELETE":
            try:
                os.remove(path)
            except OSError:
                pass
            return self.ok()
        return self.fail(405, "Method not allowed")

    def api_fs(self, method, parts, query):
        """Folders on this computer, for the "open a folder" picker."""
        return self.ok(projects.list_dirs((query.get("path") or [""])[0]))

    def api_java(self, method, parts, query):
        """The Java tools: setup, one language server per project, and the editor's requests to it."""
        h = self.hub
        J = h.java
        b = self.body() if method == "POST" else {}
        pid = (query.get("project") or [""])[0] or b.get("project") or ""
        proj = h.projects.get(pid) if pid else None
        if pid and not proj:
            return self.fail(404, "That project was removed.")
        if method == "GET":
            out = {"tools": J.tools.status()}
            if proj:
                s = J.servers.get(pid)
                out["is_java"] = J.is_java_project(proj.root)
                out["server"] = s.info() if s else {"state": "stopped", "message": "", "root_uri": javals.file_uri(proj.root), "counts": {}, "diagnostics": []}
            return self.ok(out)
        act = b.get("action")
        if act == "setup":
            J.tools.javas(refresh=True)
            J.tools.install()
            return self.ok({"tools": J.tools.status()})
        if not proj:
            return self.fail(400, "Pick a project.")
        try:
            if act == "start":
                return self.ok({"server": J.server(pid, proj.root).info()})
            if act in ("stop", "restart"):
                s = J.servers.get(pid)
                if s:
                    s.stop()
                if act == "restart":
                    J.servers.pop(pid, None)
                    return self.ok({"server": J.server(pid, proj.root).info()})
                return self.ok()
            s = J.servers.get(pid)
            if act == "test":
                cmd = J.test_command(proj.root, str(b.get("cls") or ""), b.get("method") or None, b.get("port") or None)
                return self.ok({"command": cmd}) if cmd else self.fail(400, "This project has no Maven, Gradle or Ant build to run tests with.")
            if not s or s.state in ("stopped", "error"):
                return self.fail(409, "The Java language server isn't running.")
            if act == "sync":
                s.sync(b.get("op"), str(b.get("uri") or ""), b.get("text"), b.get("version"))
                return self.ok()
            if act == "call":
                return self.ok({"result": s.call(str(b.get("method") or ""), b.get("params"), timeout=min(int(b.get("timeout") or 30), 120))})
            if act == "apply":     # an edit from the page (rename, quick fix) for files that aren't open in it
                return self.ok({"changed": s.apply_edit(b.get("edit") or {}, from_page=True)})
            if act == "debug":
                return self.java_debug(h, pid, proj, s, b)
        except javals.LspError as e:
            return self.fail(400, str(e))
        return self.fail(400, "Unknown action")

    def java_debug(self, h, pid, proj, server, b):
        """Start a debug session (a main method, or one test through the build tool) and drive it."""
        op = b.get("op")
        root = os.path.realpath(proj.root)

        def abspath(rel):
            p = os.path.realpath(os.path.join(root, rel))
            if not p.startswith(root + os.sep):
                raise javadebug.DebugError("That file isn't in this project.")
            return p
        bps = {abspath(k): v for k, v in (b.get("breakpoints") or {}).items() if isinstance(v, list)}
        cur = h.debug.get(pid)
        try:
            if op in ("launch", "test"):
                if cur and cur.state != "ended":
                    cur.control("stop")
                d = h.debug[pid] = javadebug.Session(server, pid, h.publish)

                def fail(e):
                    h.publish({"type": "debug", "project": pid, "kind": "output", "category": "stderr", "text": "Debugging couldn't start: %s\n" % e})
                    d._end()
                if op == "launch":
                    cls = str(b.get("cls") or "")

                    def go():
                        try:
                            d.launch(cls, b.get("args") or "", b.get("vmargs") or "", bps)
                        except Exception as e:  # noqa: BLE001 - reported to the page
                            fail(e)
                    threading.Thread(target=go, daemon=True).start()
                    return self.ok({"started": True})
                # a test: the build tool starts it waiting for a debugger; attach when it says it's listening
                port = javadebug.free_port()
                cmd = h.java.test_command(root, str(b.get("cls") or ""), b.get("method") or None, port)
                if not cmd:
                    return self.fail(400, "This project has no Maven, Gradle or Ant build to run tests with.")
                seen = {"text": "", "done": False}
                label = (str(b.get("cls") or "").split(".")[-1] + ("." + b["method"] if b.get("method") else ""))

                def watch(ev):
                    h.publish(ev)
                    if ev.get("type") == "term" and not seen["done"]:
                        seen["text"] = (seen["text"] + ev.get("data", ""))[-4000:]
                        m = re.search(r"Listening for transport dt_socket at address:\s*(\d+)", seen["text"])
                        if m:
                            seen["done"] = True
                            threading.Thread(target=lambda: _attach(int(m.group(1))), daemon=True).start()
                        elif "T E S T S" in seen["text"] and not seen.get("probing"):
                            # Maven Surefire keeps the test JVM's "Listening" line to itself: wait for the port instead
                            seen["probing"] = True
                            threading.Thread(target=_probe, daemon=True).start()
                    elif ev.get("type") == "term_done" and not seen["done"]:
                        seen["done"] = True
                        fail("the test run ended before the debugger could attach (see the Terminal)")

                def _probe():
                    for _ in range(240):
                        if seen["done"]:
                            return
                        if javadebug.port_open(port):
                            seen["done"] = True
                            return _attach(port)
                        time.sleep(0.5)

                def _attach(p):
                    try:
                        d.attach(p, label, bps)
                    except Exception as e:  # noqa: BLE001 - reported to the page
                        fail(e)
                run = h.projects.term_start(pid, cmd, watch, test=True)
                return self.ok({"command": cmd, "run": run})
            if not cur:
                return self.fail(409, "No debug session.")
            if op == "breakpoints":
                return self.ok({"breakpoints": cur.set_breakpoints(abspath(str(b.get("path") or "")), b.get("lines") or [])})
            if op == "control":
                cur.control(str(b.get("what") or ""), b.get("thread"))
                return self.ok()
            if op == "frame":
                return self.ok(cur.frame(int(b.get("frame"))))
            if op == "variables":
                return self.ok({"vars": cur.variables(int(b.get("ref")))})
            if op == "evaluate":
                return self.ok(cur.evaluate(str(b.get("expression") or ""), b.get("frame")))
            if op == "source":
                return self.ok({"content": cur.source(b.get("ref"))})
        except (javadebug.DebugError, javals.LspError, ValueError) as e:
            return self.fail(400, str(e))
        return self.fail(400, "Unknown debug action")

    def api_projects(self, method, parts, query):
        h = self.hub
        P = h.projects
        if not parts:
            if method == "GET":
                return self.ok({"projects": [P.summary(p) for p in P.all()], "git": bool(gitops.git_path()),
                                "default_parent": projects.default_parent()})
            if method == "POST":
                b = self.body()
                act = b.get("action")
                try:
                    if act == "open":
                        d = P.add_folder(b.get("path"))
                    elif act == "clone":
                        d = P.clone(b.get("url"), b.get("parent"))
                    elif act == "zip":
                        d = P.import_zip(b.get("filename"), base64.b64decode(b.get("data") or ""), b.get("parent"))
                    else:
                        return self.fail(400, "Unknown action.")
                except gitops.GitError as e:
                    return self.fail(400, str(e))
                return self.ok({"project": P.summary(d), "projects": [P.summary(p) for p in P.all()]})
            return self.fail(405, "Method not allowed")
        p = P.get(parts[0])
        if not p:
            return self.fail(404, "That project was removed.")
        sub = parts[1] if len(parts) > 1 else ""
        if not sub:
            if method == "PATCH":
                P.update(p.id, self.body())
                return self.ok({"project": P.summary(p.data)})
            if method == "DELETE":   # forget the project; its files stay where they are
                P.term_stop(p.id)
                P.remove(p.id)
                for c in h.store.chats:
                    if c.get("project") == p.id:
                        c["project"] = ""
                h.save_chats()
                return self.ok()
            return self.ok({"project": P.summary(p.data)})
        if not os.path.isdir(p.root):
            return self.fail(404, "The folder %s isn't there any more. Was it moved or deleted?" % p.root)
        try:
            return self.project_action(p, sub, method, query)
        except projects.ToolError as e:
            return self.fail(400, str(e))
        except gitops.GitError as e:
            return self.fail(409, str(e))

    def project_action(self, p, sub, method, query):
        h = self.hub
        P = h.projects
        arg = lambda k: (query.get(k) or [""])[0]
        if sub == "files" and method == "GET":
            return self.ok({"files": p.files(), "root": p.root})
        if sub == "tasks" and method == "GET":
            return self.ok(tasks.detect(p.root))
        if sub == "file":
            if method == "GET":
                full = p.resolve(arg("path"), allow_root=False)
                try:
                    text, fmt = projects.read_text(full)
                except projects.ToolError as e:
                    return self.ok({"path": p.rel(full), "text": None, "reason": str(e)})
                return self.ok({"path": p.rel(full), "text": text, "crlf": fmt["crlf"],
                                "mtime": os.path.getmtime(full)})
            if method == "PUT":
                b = self.body()
                full = p.resolve(b.get("path"), allow_root=False)
                if os.path.isdir(full):
                    return self.fail(400, "That's a folder.")
                exists = os.path.isfile(full)
                if exists and b.get("mtime") and abs(os.path.getmtime(full) - float(b["mtime"])) > 0.001 \
                        and not b.get("force"):
                    return self.fail(409, "This file changed on disk since you opened it (maybe the AI or another "
                                          "program changed it). Reload it, or save again to overwrite.")
                fmt = projects.read_text(full)[1] if exists else {}
                if b.get("create") and exists:
                    return self.fail(409, "%s already exists." % p.rel(full))
                projects.write_text(full, str(b.get("text") or ""), fmt)
                return self.ok({"path": p.rel(full), "mtime": os.path.getmtime(full)})
            if method == "DELETE":
                full = p.resolve(arg("path"), allow_root=False)
                if os.path.isfile(full):
                    os.remove(full)
                return self.ok()
        if sub == "git":
            if method == "GET":
                if not gitops.git_path():
                    return self.ok({"is_repo": False, "no_git": True})
                if not p.is_git():
                    return self.ok({"is_repo": False})
                st = gitops.status(p.root)
                st["branches"] = gitops.branches(p.root)
                st["remotes"] = gitops.remotes(p.root)
                st["log"] = gitops.log(p.root, 20)
                return self.ok(st)
            b = self.body()
            op = b.get("op")
            out = ""
            if op == "init":
                gitops.init(p.root)
            elif op == "fetch":
                gitops.fetch(p.root)
            elif op == "pull":
                out = gitops.pull(p.root, b.get("mode") if b.get("mode") in ("ff", "merge", "rebase") else "ff")
            elif op == "push":
                pv = gitops.push(p.root)
                out = "Pushed %d commit%s to %s/%s." % (len(pv["commits"]), "" if len(pv["commits"]) == 1 else "s",
                                                        pv["remote"], pv["branch"])
            elif op == "commit":
                c = gitops.commit(p.root, b.get("message"), [x for x in b.get("paths") or [] if isinstance(x, str)] or None)
                out = "Committed %s: %s" % (c["hash"], c["subject"])
            elif op == "switch":
                gitops.switch(p.root, b.get("name"), bool(b.get("create")))
            elif op == "abort":
                gitops.abort(p.root)
            elif op == "continue":
                gitops.finish(p.root)
            elif op == "discard":
                rel = p.rel(p.resolve(b.get("path"), allow_root=False))
                gitops.discard(p.root, rel)
            else:
                return self.fail(400, "Unknown Git action.")
            return self.ok({"output": out.strip()})
        if sub == "push_preview" and method == "GET":
            return self.ok(gitops.push_preview(p.root))
        if sub == "diff" and method == "GET":
            path = arg("path")
            if path:
                full = p.resolve(path, allow_root=False)
                rel = p.rel(full)
                st = gitops.status(p.root)
                f = next((x for x in st["files"] if x["path"] == rel), None)
                if f and f["code"] == "?":
                    try:
                        return self.ok({"diff": gitops.diff(p.root, rel, untracked_text=projects.read_text(full)[0])})
                    except projects.ToolError as e:
                        return self.ok({"diff": "", "reason": str(e)})
                return self.ok({"diff": gitops.diff(p.root, rel)})
            return self.ok({"diff": gitops.diff(p.root)})
        if sub == "terminal" and method == "POST":
            b = self.body()
            if b.get("stop"):
                P.term_stop(p.id)
                return self.ok()
            return self.ok({"run": P.term_start(p.id, b.get("command"), h.publish, bool(b.get("test")),
                                                b.get("suggestion") or None)})
        if sub == "tests":
            T = P.tests
            if method == "GET":
                d = T.get(p.id)
                return self.ok(dict(d, test_command=p.data.get("test_command", ""), guess=projects.testlog.guess_command(p.root)))
            b = self.body()
            act = b.get("action")
            if act == "add":
                added = T.suggest(p.id, [b], "you")
                if not added:
                    return self.fail(400, "Give the test a title (or it's already on the list).")
                return self.ok({"suggestion": added[0]})
            if act == "update":
                return self.ok({"suggestion": T.update(p.id, b.get("id"), b)})
            if act == "delete":
                T.delete(p.id, sid=b.get("id"))
                return self.ok()
            if act == "clear_runs":
                T.delete(p.id, runs=True)
                return self.ok()
            if act == "suggest":
                return self.ok({"added": T.suggest(p.id, self.suggest_tests(p, b), "ai")})
            return self.fail(400, "Unknown Tests action.")
        if sub == "undo" and method == "POST":
            return self.ok({"path": P.undo(self.body().get("change"))})
        if sub == "commit_message" and method == "POST":
            return self.ok({"message": self.commit_message(p, self.body())})
        return self.fail(404, "Unknown project action")

    def ask_model(self, b, prompt):
        """One quick, non-streaming answer from the current model (Fast mode, no tools)."""
        h = self.hub
        model = b.get("model") or h.store.settings.get("last_model")
        cid, name = engine.split_model(model)
        conn = h.store.conn(cid)
        if not conn:
            raise ValueError("Pick a model in Settings first.")
        out = []
        providers.create(conn).chat(name, [{"role": "user", "content": prompt}], {"temperature": 0.2, "fast": True},
                                    lambda t, th: out.append(t), threading.Event())
        return "".join(out).split("</think>")[-1].strip()

    def suggest_tests(self, p, b):
        """Ask the model which tests are missing, from the project's layout, its test files and recent changes."""
        tree, _ = p.outline(120)
        files = p.files()
        tests = [f for f in files if projects.re.search(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|_test\.|\.(test|spec)\.", f)]
        changed = ""
        if gitops.git_path() and p.is_git():
            try:
                changed = projects.clip(gitops.diff(p.root), 6000)
            except gitops.GitError:
                pass
        samples = []
        for f in [x for x in files if x not in tests and projects.re.search(r"\.(py|js|ts|tsx|jsx|go|rs|java|cs|rb|php|kt|swift)$", x)][:6]:
            try:
                samples.append("--- %s\n%s" % (f, projects.read_text(projects.os.path.join(p.root, f))[0][:2500]))
            except projects.ToolError:
                pass
        have = [s["title"] for s in self.hub.projects.tests.get(p.id)["suggested"]]
        prompt = ("You review a code project and list tests that are missing or worth running. Reply with 3 to 8 lines, "
                  "each exactly in this form and nothing else:\nTEST: <what to test> | <why it matters, one sentence> | "
                  "<file the test belongs in>\n\nProject layout:\n" + tree +
                  "\n\nExisting test files: " + (", ".join(tests[:60]) or "none") +
                  ("\n\nAlready suggested (don't repeat): " + "; ".join(have[:40]) if have else "") +
                  ("\n\nUncommitted changes:\n" + changed if changed.strip() else "") +
                  "\n\nSome source files:\n" + "\n\n".join(samples))
        items = []
        for line in self.ask_model(b, prompt).splitlines():
            line = line.strip().lstrip("-*0123456789. ").strip()
            if line.upper().startswith("TEST:"):
                line = line[5:]
            parts = [x.strip().strip("`") for x in line.split("|")]
            if len(parts) >= 2 and parts[0]:
                items.append({"title": parts[0], "why": parts[1], "file": parts[2] if len(parts) > 2 else ""})
        if not items:
            raise ValueError("The model didn't suggest any tests in a form the app understands. Try again or another model.")
        return items

    def commit_message(self, p, b):
        """Ask the current model for a commit message describing the uncommitted changes."""
        h = self.hub
        model = b.get("model") or h.store.settings.get("last_model")
        cid, name = engine.split_model(model)
        conn = h.store.conn(cid)
        if not conn:
            raise ValueError("Pick a model in Settings first.")
        paths = [x for x in b.get("paths") or [] if isinstance(x, str)]
        st = gitops.status(p.root)
        files = [f for f in st["files"] if not paths or f["path"] in paths]
        if not files:
            raise ValueError("There are no changes to describe.")
        parts = []
        for f in files[:40]:
            if f["code"] == "?":
                try:
                    parts.append(gitops.diff(p.root, f["path"], untracked_text=projects.read_text(
                        os.path.join(p.root, f["path"]))[0]))
                except projects.ToolError:
                    parts.append("new binary file " + f["path"])
            else:
                parts.append(gitops.diff(p.root, f["path"]))
        diff = projects.clip("\n".join(parts), 14000)
        prompt = ("Write a Git commit message for these changes. First line: a short summary in the imperative "
                  "mood, at most 60 characters, no full stop. If the change needs it, add a blank line and a few "
                  "short bullet points. Reply with only the message, no code fences.\n\n" + diff)
        out = []
        cancel = threading.Event()
        providers.create(conn).chat(name, [{"role": "user", "content": prompt}],
                                    {"temperature": 0.2, "fast": True}, lambda t, th: out.append(t), cancel)
        text = "".join(out).strip()
        text = text.split("</think>")[-1].strip().strip("`").strip()
        return text

    def api_http(self, method, parts, query):
        """The API tab: requests, collections, environments, history, cookies, auth, runs, endpoints, dev server."""
        h = self.hub
        A = h.api
        sub = parts[0] if parts else ""
        arg = lambda k: (query.get(k) or [""])[0]
        b = self.body() if method in ("POST", "PUT", "PATCH") else {}
        pid = b.get("project", arg("project")) or ""
        proj = h.projects.get(pid) if pid else None
        if pid and not proj:
            return self.fail(404, "That project was removed.")

        def env_for():
            eid = b.get("env")
            return next((e for e in A.data["environments"] if e["id"] == eid), None) if eid else A.env(pid)

        # the dev server's address is the fallback {{baseUrl}}; a collection or environment value wins
        defaults = {"baseUrl": proj.server_config()["url"].rstrip("/")} if proj is not None else {}

        def placed(req):
            """(collection, chain) for a request being edited: where it lives, for inherited auth and scripts."""
            col = A.collection(b.get("collection") or "")
            if not col:
                return None, None
            node, chain = apirun.api.find(col, req.get("id"))
            if node is None and b.get("parent"):
                parent, chain = apirun.api.find(col, b["parent"])
                chain = chain if parent is not None and parent.get("folder") else [col]
            if node is None:
                chain = (chain or [col]) + [req]
            return col, chain

        if proj is not None:
            A.load_shared(pid, proj.root)       # keep collections from the project's .api folder current
        if sub == "state" and method == "GET":
            out = A.scope(pid, proj.root if proj else None)
            out["node"] = bool(shutil.which("node"))
            if proj is not None:
                out["server"] = dict(h.servers.status(pid), config=proj.server_config(), saved=proj.data.get("dev_server") or {})
                out["tasks"] = tasks.detect(proj.root)
            return self.ok(out)
        if sub == "send" and method == "POST":
            req = b.get("request") or {}
            col, chain = placed(req)
            runner = apirun.Runner(A, pid, h.run_script, defaults=defaults)
            env = env_for()
            r = runner.execute(col, req, chain, env)
            res = r["response"]
            A.add_history(pid, req, res, "you")
            res["sent_headers"] = [[k, v] for k, v in res.get("sent_headers") or []]
            out = dict(res, checks=r["checks"], tests=r["tests"], console=r["console"], script_errors=r["script_errors"])
            return self.ok(out)
        if sub == "script" and method == "POST":       # a script result from the app window's sandbox
            box = h.script_calls.get(b.get("id"))
            if box and not box["event"].is_set():
                box["result"] = b.get("result")
                box["event"].set()
            return self.ok()
        if sub == "code" and method == "POST":
            req = b.get("request") or {}
            col, chain = placed(req)
            runner = apirun.Runner(A, pid, defaults=defaults)
            vals = runner.merged(runner.scopes(col, env_for()))
            auth = apirun.api.effective_auth(chain) if chain else req.get("auth")
            return self.ok({"code": api.codegen(b.get("lang") or "curl", req, vals, auth, A)})
        if sub == "rt" and method == "POST":
            act = b.get("action")
            if act == "open":
                req = b.get("request") or {}
                runner = apirun.Runner(A, pid, defaults=defaults)
                col, chain = placed(req)
                vals = runner.merged(runner.scopes(col, env_for()))
                url = api.fill(req.get("url"), vals).strip()
                if req.get("kind") == "ws" and url.startswith("http"):
                    url = "ws" + url[4:]
                hdrs = [(api.fill(x["key"], vals), api.fill(x.get("value"), vals)) for x in req.get("headers") or []
                        if x.get("enabled", True) and x.get("key")]
                auth = apirun.api.effective_auth(chain) if chain else req.get("auth") or {}
                if auth.get("type") == "bearer" and auth.get("token"):
                    hdrs.append(("Authorization", "Bearer " + api.fill(auth["token"], vals)))
                try:
                    return self.ok(h.rt.open(req.get("kind"), url, hdrs, api.fill(req.get("protocols"), vals),
                                             verify=A.settings().get("verify", True)))
                except (OSError, ConnectionError, ValueError) as e:
                    return self.fail(400, "Couldn't connect: %s" % (getattr(e, "reason", None) or e))
            if act == "send":
                h.rt.send(b.get("id"), str(b.get("text") or ""))
                return self.ok()
            if act == "close":
                h.rt.close(b.get("id"))
                return self.ok()
        if sub == "files" and method == "POST":
            return self.ok({"file": A.add_file(b.get("name"), base64.b64decode(b.get("data") or ""))})
        if sub == "collections":
            cid = parts[1] if len(parts) > 1 else ""
            if method == "POST" and not cid:
                col = api.blank_collection(str(b.get("name") or "New collection")[:100], pid)
                if isinstance(b.get("items"), list):
                    col["items"] = b["items"]
                with A.lock:
                    A.data["collections"].append(col)
                    A.save()
                return self.ok({"collection": col})
            col = A.collection(cid)
            if not col:
                return self.fail(404, "That collection is gone.")
            if method == "PUT":
                with A.lock:
                    for k in ("name", "description", "pre_script", "test_script"):
                        if k in b:
                            col[k] = str(b[k])[:200 if k == "name" else 200000] or (col[k] if k == "name" else "")
                    for k in ("items", "variables"):
                        if isinstance(b.get(k), list):
                            col[k] = b[k]
                    if isinstance(b.get("auth"), dict):
                        col["auth"] = b["auth"]
                    A.persist(col)
                return self.ok({"collection": col})
            if method == "DELETE":
                with A.lock:
                    if col.get("location") == "project":
                        A.move(col, "app")
                    A.data["collections"] = [c for c in A.data["collections"] if c["id"] != cid]
                    A.save()
                return self.ok()
            if method == "POST" and len(parts) > 2 and parts[2] == "move":
                return self.ok({"collection": A.move(col, b.get("to"), proj.root if proj else None)})
            if method == "GET" and len(parts) > 2 and parts[2] == "export":
                safe = "".join(ch for ch in col["name"] if ch.isalnum() or ch in " -_").strip() or "collection"
                return self.send(200, json.dumps(api.to_postman(col), indent=2, ensure_ascii=False), "application/json; charset=utf-8",
                                 {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe + ".postman_collection.json")})
            if method == "POST" and len(parts) > 2 and parts[2] == "run":
                folder = apirun.api.find(col, b.get("folder"))[0] if b.get("folder") else None
                try:
                    data = apirun.parse_data(b.get("data") or "", b.get("data_name") or "")
                except (ValueError, csv.Error) as e:
                    return self.fail(400, "The data file couldn't be read: %s" % e)
                env = env_for()
                run_id = b.get("run") or ""

                def progress(res):
                    if not run_id:
                        return
                    r = res.get("response") or {}
                    h.publish({"type": "api_progress", "run": run_id, "result": {
                        "name": res.get("name"), "iteration": res.get("iteration"), "skipped": res.get("skipped"),
                        "pass": res.get("pass"), "checks": res.get("checks"), "tests": res.get("tests"),
                        "script_errors": res.get("script_errors"),
                        "response": {k: r.get(k) for k in ("method", "status", "ms", "error")}}})
                rep = apirun.Runner(A, pid, h.run_script, defaults=defaults).run(col, env, folder, int(b.get("iterations") or 1), data,
                                                              int(b.get("delay") or 0), bool(b.get("bail")), progress)
                secrets = api.variables(env)[1]
                if pid:
                    h.projects.tests.add_run(pid, "API: " + (folder or col)["name"], 1 if rep["failed"] else 0,
                                             apirun.text_report(rep, secrets), "you", rep["seconds"])
                rid = api.new_id()
                h.api_reports[rid] = rep
                for k in list(h.api_reports)[:-20]:
                    del h.api_reports[k]
                return self.ok(dict(json.loads(apirun.json_report(rep)), report=rid))
        if sub == "reports" and method == "GET" and len(parts) > 1:
            rep = h.api_reports.get(parts[1])
            if not rep:
                return self.fail(404, "That report is gone; run the collection again.")
            fmt = arg("format") or "html"
            body, ctype, ext = {"junit": (apirun.junit_report(rep), "application/xml", "xml"),
                                "json": (apirun.json_report(rep), "application/json", "json")}.get(fmt, (apirun.html_report(rep), "text/html", "html"))
            name = "".join(ch for ch in rep["name"] if ch.isalnum() or ch in " -_").strip() or "report"
            return self.send(200, body, ctype + "; charset=utf-8",
                             {"Content-Disposition": "attachment; filename*=UTF-8''" + quote("%s report.%s" % (name, ext))})
        if sub == "import" and method == "POST":
            text = (b.get("text") or "").strip()
            target = A.collection(b.get("collection") or "")
            if text.startswith("curl"):
                r = api.from_curl(text)
                with A.lock:
                    if not target:
                        target = api.blank_collection("Imported", pid)
                        A.data["collections"].append(target)
                    target["items"].append(r)
                    A.save()
                return self.ok({"collection": target, "request": r})
            try:
                doc = json.loads(text)
            except ValueError:
                try:
                    doc = api.load_yaml(text)
                except ValueError:
                    return self.fail(400, "Paste a curl command, a Postman collection or environment, or an OpenAPI spec (JSON or YAML).")
            with A.lock:
                if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc):
                    col, env = api.from_openapi(doc)
                elif isinstance(doc, dict) and "values" in doc and "item" not in doc:
                    env = api.env_from_postman(doc)
                    env["project"] = pid
                    A.data["environments"].append(env)
                    A.save()
                    return self.ok({"environment": env})
                else:
                    col, env = api.from_postman(doc)
                col["project"] = pid
                A.data["collections"].append(col)
                if env:
                    env["project"] = pid
                    A.data["environments"].append(env)
                A.save()
            return self.ok({"collection": col, "environment": env})
        if sub == "environments":
            eid = parts[1] if len(parts) > 1 else ""
            with A.lock:
                if method == "POST" and not eid:
                    env = {"id": api.new_id(), "name": str(b.get("name") or "New environment")[:100], "project": pid,
                           "vars": b.get("vars") if isinstance(b.get("vars"), list) else
                           [{"key": "baseUrl", "value": proj.server_config()["url"] if proj else "http://localhost:3000",
                             "secret": False, "enabled": True}]}
                    A.data["environments"].append(env)
                    A.data["active_env"][pid] = env["id"]
                    A.save()
                    return self.ok({"environment": env})
                env = next((e for e in A.data["environments"] if e["id"] == eid), None)
                if not env:
                    return self.fail(404, "That environment is gone.")
                if method == "GET" and len(parts) > 2 and parts[2] == "export":
                    return self.send(200, json.dumps(api.env_to_postman(env), indent=2, ensure_ascii=False), "application/json; charset=utf-8",
                                     {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(env["name"] + ".postman_environment.json")})
                if method == "PUT":
                    if "name" in b:
                        env["name"] = str(b["name"])[:100] or env["name"]
                    if isinstance(b.get("vars"), list):
                        env["vars"] = [{"key": str(v.get("key") or "").strip()[:100], "value": str(v.get("value") or ""),
                                        "secret": bool(v.get("secret")), "enabled": v.get("enabled", True) is not False}
                                       for v in b["vars"] if isinstance(v, dict) and str(v.get("key") or "").strip()]
                    A.save()
                    return self.ok({"environment": env})
                if method == "DELETE":
                    A.data["environments"] = [e for e in A.data["environments"] if e["id"] != eid]
                    A.save()
                    return self.ok()
        if sub == "globals" and method == "PUT":
            with A.lock:
                A.data["globals"][pid] = [{"key": str(v.get("key") or "").strip(), "value": str(v.get("value") or ""), "enabled": True}
                                          for v in b.get("vars") or [] if isinstance(v, dict) and str(v.get("key") or "").strip()]
                A.save()
            return self.ok()
        if sub == "active_env" and method == "POST":
            with A.lock:
                A.data["active_env"][pid] = b.get("env") or ""
                A.save()
            return self.ok()
        if sub == "settings" and method == "PUT":
            with A.lock:
                s = A.data["settings"]
                for k in ("timeout_ms",):
                    if k in b:
                        s[k] = max(100, min(int(b[k] or 30000), 600000))
                for k in ("follow", "verify", "cookies"):
                    if k in b:
                        s[k] = bool(b[k])
                for k in ("proxy", "cert", "key", "passphrase"):
                    if k in b:
                        s[k] = str(b[k] or "").strip()
                A.save()
            return self.ok({"settings": A.settings()})
        if sub == "history" and method == "DELETE":
            with A.lock:
                A.data["history"] = [x for x in A.data["history"] if x.get("project", "") != pid]
                A.save()
            return self.ok()
        if sub == "cookies":
            jar = A.jar(pid)
            if method == "GET":
                return self.ok({"cookies": [{"domain": c.domain, "path": c.path, "name": c.name, "value": c.value or "",
                                             "expires": c.expires, "secure": c.secure} for c in jar]})
            if method == "DELETE":
                dom, name = arg("domain"), arg("name")
                if dom and name:
                    try:
                        jar.clear(dom, arg("path") or "/", name)
                    except KeyError:
                        pass
                elif dom:
                    try:
                        jar.clear(dom)
                    except KeyError:
                        pass
                else:
                    jar.clear()
                A.save_jar(pid)
                return self.ok()
        if sub == "oauth" and method == "POST":
            req = b.get("request") or {}
            col, chain = placed(req)
            auth = apirun.api.effective_auth(chain) if chain else req.get("auth") or {}
            if auth.get("type") != "oauth2":
                return self.fail(400, "This request doesn't use OAuth 2.0.")
            runner = apirun.Runner(A, pid, defaults=defaults)
            vals = runner.merged(runner.scopes(col, env_for()))
            if b.get("action") == "clear":
                with A.lock:
                    A.data["tokens"].pop(api._token_key(auth, vals), None)
                    A.save()
                return self.ok()
            if (auth.get("grant") or "client_credentials") in ("client_credentials", "password"):
                tok = api.oauth_token(A, auth, vals, force=True)
                return self.ok({"token": tok[:12] + "…", "done": True})
            return self.ok({"url": h.oauth_begin(auth, vals, "http://127.0.0.1:%d/oauth/callback" % self.server.server_address[1])})
        if proj is None and sub in ("endpoints", "compare", "server", "docs"):
            return self.fail(400, "Pick a project first.")
        if sub == "endpoints" and method == "GET":
            return self.ok({"routes": api.find_routes(proj)})
        if sub == "docs" and method == "GET":
            # documentation files to compare with: Markdown/text docs, the likely API ones first
            files = [f for f in proj.files() if re.search(r"\.(md|markdown|txt|rst|adoc)$", f, re.I)]
            def score(f):
                n = f.lower()
                return (0 if "api" in n else 1 if n.startswith("docs/") or "/docs/" in n else 2 if "readme" in n else 3, n)
            return self.ok({"files": sorted(files, key=score)[:200]})
        if sub == "compare" and method == "POST":
            docs = []
            for f in (b.get("files") or [])[:30]:
                try:
                    docs.append((f, projects.read_text(proj.resolve(f, allow_root=False))[0]))
                except projects.ToolError:
                    pass
            if not docs:
                return self.fail(400, "Pick at least one documentation file.")
            result = api.compare(api.find_routes(proj), docs)
            if b.get("suggest"):
                items = [{"title": "Document %s %s" % (r["method"], r["path"]), "file": docs[0][0],
                          "why": "It's in the code (%s:%s) but not in the docs." % (r["file"], r["line"])} for r in result["undocumented"]]
                items += [{"title": "Docs mention %s %s, which isn't in the code" % (d["method"], d["path"]), "file": d["file"],
                           "why": "Remove it from %s or add the endpoint." % d["file"]} for d in result["stale"]]
                h.projects.tests.suggest(pid, items, "you")
            return self.ok(result)
        if sub == "server" and method == "POST":
            act = b.get("action")
            if "command" in b or "url" in b:
                h.projects.update(pid, {"dev_server": {"command": b.get("command", ""), "url": b.get("url", "")}})
                proj = h.projects.get(pid)
            cfg = proj.server_config()
            if act == "start":
                h.servers.stop(pid)
                h.servers.start(pid, cfg["command"], cfg["url"], proj.root)
            elif act == "stop":
                h.servers.stop(pid)
            return self.ok(dict(h.servers.status(pid), config=cfg))
        return self.fail(404, "Unknown API action")

    def api_agent(self, method, parts, query):
        """Open the agent browser window, e.g. so the user can log in to sites first."""
        if method == "POST" and parts == ["open"]:
            b = engine.agent_for(self.hub.store)
            b.show()
            return self.ok({"profile": b.profile})
        if method == "GET" and parts == ["status"]:
            b = engine.agent_for(self.hub.store)
            return self.ok({"running": b.alive(), "profile": b.profile,
                            "chrome": agent_browser.find_chrome(self.hub.store.settings.get("agent", {}).get("chrome_path", ""))})
        return self.fail(404, "Unknown agent action")

    def api_library(self, method, parts, query):
        lib = self.hub.library
        if not parts or parts[0] not in KINDS:
            return self.fail(404, "Unknown library section.")
        kind = parts[0]
        name = (query.get("name") or [""])[0]
        if len(parts) > 1 and parts[1] == "import" and method == "POST":
            b = self.body()
            tmpdir = tempfile.mkdtemp()
            try:
                fname = os.path.basename(b.get("filename") or "upload.txt") or "upload.txt"
                p = os.path.join(tmpdir, fname)
                with open(p, "wb") as f:
                    f.write(base64.b64decode(b.get("data") or ""))
                stored = lib.import_file(kind, p)
                if kind == "standards":   # an uploaded standard is in force straight away
                    lib.set_meta(stored, {"use_for": os.path.splitext(stored)[0], "files": "", "active": True})
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return self.ok({"name": stored, "names": lib.names(kind)})
        if method == "GET":
            if name:
                out = {"name": name, "text": lib.read(kind, name)}
                if kind == "standards":
                    out["meta"] = lib.meta(name)
                return self.ok(out)
            out = {"names": lib.names(kind), "folder": lib.dir(kind)}
            if kind == "standards":
                out["metas"] = {n: lib.meta(n) for n in out["names"]}
            return self.ok(out)
        if method == "PUT":
            b = self.body()
            old, new = b.get("old") or "", b.get("name") or ""
            if old and old in lib.names(kind) and new != old:
                new = lib.rename(kind, old, new)
                self.rename_in_chats(kind, old, new)
            stored = lib.save(kind, new, b.get("text") or "")
            if kind == "standards":
                lib.set_meta(stored, b.get("meta") if isinstance(b.get("meta"), dict) else lib.meta(stored))
            return self.ok({"name": stored, "names": lib.names(kind)})
        if method == "DELETE" and name:
            lib.delete(kind, name)
            return self.ok({"names": lib.names(kind)})
        return self.fail(405, "Method not allowed")

    def rename_in_chats(self, kind, old, new):
        h = self.hub
        for c in h.store.chats:
            if kind == "documents" and old in c.get("docs", []):
                c["docs"] = [new if d == old else d for d in c["docs"]]
            if kind == "templates" and c.get("template") == old:
                c["template"] = new
        h.save_chats()

    def api_backup(self, method, parts, query):
        h = self.hub
        if method == "GET":
            data = json.dumps({"version": 2, "chats": h.store.chats}, ensure_ascii=False, indent=1)
            return self.send(200, data, "application/json; charset=utf-8",
                             {"Content-Disposition": "attachment; filename=vscode-assistant-backup.json"})
        if method == "POST":
            n = engine.import_chats(h.store, self.body())
            h.save_chats()
            return self.ok({"added": n})
        return self.fail(405, "Method not allowed")


def find_chrome():
    """Path to Chrome/Chromium/Edge if installed, so the app opens there."""
    cands = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                          os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands += [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable", "chromium",
                                                  "chromium-browser", "microsoft-edge")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def open_browser(url, app_window=True):
    chrome = find_chrome()
    if chrome:
        args = [chrome, "--app=" + url] if app_window else [chrome, url]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def already_running(port):
    """True if this app is already serving on the port (a second launch just reopens it)."""
    try:
        with net.open_url("http://127.0.0.1:%d/" % port, timeout=2) as r:
            return r.headers.get("Server", "").startswith(Handler.server_version)
    except Exception:
        return False


def say(text):
    if sys.stdout is not None:  # pythonw (run.pyw) has no console
        print(text, flush=True)


def serve(port=8765, open_page=True, app_window=True, idle_exit=True):
    if already_running(port):
        url = "http://127.0.0.1:%d/" % port
        say("%s is already running at %s" % (APP_NAME, url))
        if open_page:
            open_browser(url, app_window)
        return
    hub = Hub()
    Handler.hub = hub
    httpd = None
    for p in [port] + list(range(port + 1, port + 20)) + [0]:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("Couldn't open a local port.")
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    say("%s is running at %s  (Ctrl+C to stop)" % (APP_NAME, url))
    if open_page:
        threading.Timer(0.4, open_browser, args=(url, app_window)).start()
    if idle_exit:
        def watchdog():
            # once a page has connected, stop when every app window has been closed for a while
            # and nothing is generating, so a double-clicked run.pyw doesn't linger
            while True:
                time.sleep(5)
                with hub.lock:
                    idle = hub.seen_client and not hub.clients and time.time() - hub.last_client > IDLE_EXIT
                busy_term = any(t["proc"].poll() is None for t in hub.projects.terms.values())
                if idle and not hub.streams and not hub.pulls and not busy_term:
                    say("All app windows closed; stopping.")
                    httpd.shutdown()
                    return

        threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.servers.stop_all()       # dev servers started from the app stop with it
        for d in list(hub.debug.values()):
            d.control("stop")
        hub.java.stop_all()
        hub.rt.close_all()
        httpd.server_close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run %s in your browser." % APP_NAME)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser window")
    ap.add_argument("--tab", action="store_true", help="open as a normal browser tab instead of an app window")
    ap.add_argument("--keep-running", action="store_true", help="don't stop when the last app window closes")
    ap.add_argument("--web", action="store_true", help=argparse.SUPPRESS)  # older launch command; now the only mode
    a = ap.parse_args(argv)
    serve(a.port, not a.no_open, not a.tab, idle_exit=not (a.keep_running or a.no_open))
