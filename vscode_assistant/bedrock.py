"""Amazon Bedrock: Claude (and the other Bedrock models) through your company's AWS account.

It signs in the way the AWS CLI does, so whatever already works for you works here:
  - an AWS SSO (IAM Identity Center) profile from ~/.aws/config, using the token `aws sso login` saved;
    the app can also do that sign-in itself (Settings › Servers › Sign in), for laptops without the AWS CLI
  - a profile with a credential_process, or keys in ~/.aws/credentials, or the AWS_* environment variables
Chat goes through Bedrock's Converse API (streamed), with tool calling, so a Bedrock model works everywhere
a local one does: chats, projects, the agent browser and the Context page. Standard library only.
"""
import base64
import configparser
import datetime
import hashlib
import json
import os
import re
import struct
import subprocess
import threading
import time
import urllib.parse
import uuid
import zlib

from . import net
from .api import sigv4
from .providers import Base, _strip_think

# hosts, as templates (tests point them at a local fake)
ENDPOINTS = {
    "runtime": "https://bedrock-runtime.{region}.amazonaws.com",
    "control": "https://bedrock.{region}.amazonaws.com",
    "oidc": "https://oidc.{region}.amazonaws.com",
    "portal": "https://portal.sso.{region}.amazonaws.com",
}


class AwsAuthError(Exception):
    """The AWS sign-in is missing or has run out; the message says what to do."""


def _url(kind, region):
    return ENDPOINTS[kind].format(region=region)


def _parse_time(s):
    """An AWS timestamp ("2026-10-22T10:00:00Z", "...UTC", with an offset, or epoch ms) as epoch seconds."""
    if isinstance(s, (int, float)):
        return s / 1000.0 if s > 1e11 else float(s)
    s = (s or "").strip().replace("UTC", "+00:00")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the AWS config files ─────────────────────────────────────────────────
def config_path():
    return os.environ.get("AWS_CONFIG_FILE") or os.path.join(os.path.expanduser("~"), ".aws", "config")


def credentials_path():
    return os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.join(os.path.expanduser("~"), ".aws", "credentials")


def _ini(path):
    cp = configparser.RawConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        pass
    return cp


def _section(cp, name):
    return dict(cp.items(name)) if cp.has_section(name) else {}


def profiles():
    """Profile names from ~/.aws/config and ~/.aws/credentials, with whether each uses SSO."""
    out = {}
    cfg = _ini(config_path())
    for sec in cfg.sections():
        if sec == "default" or sec.startswith("profile "):
            name = "default" if sec == "default" else sec[8:].strip()
            d = _section(cfg, sec)
            out[name] = {"name": name, "sso": bool(d.get("sso_session") or d.get("sso_start_url")),
                         "region": d.get("region") or ""}
    for sec in _ini(credentials_path()).sections():
        out.setdefault(sec, {"name": sec, "sso": False, "region": ""})
    return sorted(out.values(), key=lambda p: (p["name"] != "default", p["name"]))


def sso_settings(conn):
    """{start_url, region, account_id, role_name, session} for a connection: from its AWS profile, or typed in."""
    name = (conn.get("profile") or "").strip() or os.environ.get("AWS_PROFILE") or "default"
    cfg = _ini(config_path())
    prof = _section(cfg, "default" if name == "default" else "profile " + name)
    out = {}
    if prof.get("sso_session"):
        sess = _section(cfg, "sso-session " + prof["sso_session"])
        out = {"session": prof["sso_session"], "start_url": sess.get("sso_start_url", ""), "region": sess.get("sso_region", "")}
    elif prof.get("sso_start_url"):
        out = {"session": "", "start_url": prof["sso_start_url"], "region": prof.get("sso_region", "")}
    if out:
        out.update(account_id=prof.get("sso_account_id", ""), role_name=prof.get("sso_role_name", ""), profile=name)
        return out
    if conn.get("start_url"):          # typed into Settings (no AWS CLI on this computer)
        return {"session": "", "start_url": conn["start_url"].strip(), "region": (conn.get("sso_region") or conn.get("region") or "us-east-1").strip(),
                "account_id": (conn.get("account_id") or "").strip(), "role_name": (conn.get("role_name") or "").strip(), "profile": ""}
    return None


# ── SSO tokens: the AWS CLI's cache, or the app's own sign-in ────────────
def _cli_cache_file(sso):
    key = sso["session"] or sso["start_url"]
    return os.path.join(os.path.expanduser("~"), ".aws", "sso", "cache", hashlib.sha1(key.encode()).hexdigest() + ".json")


class Sso:
    """The SSO bearer token for one start URL: read from the AWS CLI's cache or the app's, refreshed when it can be."""
    lock = threading.Lock()

    def __init__(self, data_dir):
        self.own = os.path.join(data_dir, "aws-sso.json")       # the app's own sign-ins, by start URL

    def _own(self):
        try:
            with open(self.own, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_own(self, start_url, tok):
        with self.lock:
            all_ = self._own()
            all_[start_url] = tok
            os.makedirs(os.path.dirname(self.own), exist_ok=True)
            tmp = self.own + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(all_, f)
            os.replace(tmp, self.own)
            try:
                os.chmod(self.own, 0o600)
            except OSError:
                pass

    def token(self, sso):
        """A valid access token, or raise AwsAuthError saying how to sign in."""
        cands = []
        cli = _cli_cache_file(sso)
        try:
            with open(cli, encoding="utf-8") as f:
                cands.append(("cli", json.load(f)))
        except (OSError, ValueError):
            pass
        mine = self._own().get(sso["start_url"])
        if mine:
            cands.append(("own", mine))
        for where, tok in cands:
            if tok.get("accessToken") and _parse_time(tok.get("expiresAt")) > time.time() + 60:
                return tok["accessToken"]
        for where, tok in cands:                                   # out of date: refresh it if we can
            fresh = self._refresh(sso, tok)
            if fresh:
                if where == "own":
                    self._save_own(sso["start_url"], fresh)
                return fresh["accessToken"]
        how = ("run  aws sso login --profile %s  in a terminal, or press Sign in" % sso["profile"]) if sso.get("profile") else "press Sign in"
        raise AwsAuthError("Your AWS sign-in has expired or there isn't one yet: %s (Settings › Servers)." % how)

    def _refresh(self, sso, tok):
        if not (tok.get("refreshToken") and tok.get("clientId") and tok.get("clientSecret")):
            return None
        if tok.get("registrationExpiresAt") and _parse_time(tok["registrationExpiresAt"]) < time.time():
            return None
        try:
            r = net.get_json(_url("oidc", sso["region"]) + "/token", "POST", {
                "clientId": tok["clientId"], "clientSecret": tok["clientSecret"], "grantType": "refresh_token",
                "refreshToken": tok["refreshToken"]}, timeout=20)
        except (net.HTTPError, ConnectionError):
            return None
        out = dict(tok, accessToken=r["accessToken"], expiresAt=_iso(time.time() + int(r.get("expiresIn") or 3600)))
        if r.get("refreshToken"):
            out["refreshToken"] = r["refreshToken"]
        return out

    def login(self, sso, on_done):
        """Start the browser sign-in (the same device flow as `aws sso login`). Returns {url, code} to show;
        a thread waits for the person to approve it, saves the token and calls on_done(error or None)."""
        base = _url("oidc", sso["region"])
        reg = net.get_json(base + "/client/register", "POST", {"clientName": "vscode-assistant", "clientType": "public",
                                                              "scopes": ["sso:account:access"]}, timeout=20)
        dev = net.get_json(base + "/device_authorization", "POST", {"clientId": reg["clientId"], "clientSecret": reg["clientSecret"],
                                                                    "startUrl": sso["start_url"]}, timeout=20)

        def wait():
            interval, deadline = int(dev.get("interval") or 5), time.time() + int(dev.get("expiresIn") or 600)
            while time.time() < deadline:
                time.sleep(interval)
                try:
                    r = net.get_json(base + "/token", "POST", {
                        "clientId": reg["clientId"], "clientSecret": reg["clientSecret"],
                        "grantType": "urn:ietf:params:oauth:grant-type:device_code", "deviceCode": dev["deviceCode"]}, timeout=20)
                except net.HTTPError as e:
                    msg = str(e)
                    if "authorization_pending" in msg or "AuthorizationPending" in msg:
                        continue
                    if "slow_down" in msg or "SlowDown" in msg:
                        interval += 5
                        continue
                    return on_done("The sign-in didn't finish: %s" % msg)
                except ConnectionError as e:
                    return on_done(str(e))
                self._save_own(sso["start_url"], {
                    "accessToken": r["accessToken"], "expiresAt": _iso(time.time() + int(r.get("expiresIn") or 3600)),
                    "refreshToken": r.get("refreshToken", ""), "clientId": reg["clientId"], "clientSecret": reg["clientSecret"],
                    "registrationExpiresAt": _iso(int(reg.get("clientSecretExpiresAt") or 0)) if reg.get("clientSecretExpiresAt") else "",
                    "region": sso["region"], "startUrl": sso["start_url"]})
                return on_done(None)
            on_done("The sign-in timed out. Press Sign in to try again.")
        threading.Thread(target=wait, daemon=True).start()
        return {"url": dev.get("verificationUriComplete") or dev.get("verificationUri"), "code": dev.get("userCode")}


# ── credentials ──────────────────────────────────────────────────────────
class Credentials:
    """Temporary or long-term AWS keys for a connection, found the way the AWS CLI finds them; cached until they expire."""
    cache = {}                    # connection key -> creds
    lock = threading.Lock()

    def __init__(self, conn, data_dir):
        self.conn, self.sso = conn, Sso(data_dir)
        self.key = json.dumps([conn.get(k) for k in ("profile", "start_url", "account_id", "role_name")])

    def get(self):
        with self.lock:
            c = self.cache.get(self.key)
            if c and (not c.get("expires") or c["expires"] > time.time() + 300):
                return c
        c = self._resolve()
        with self.lock:
            self.cache[self.key] = c
        return c

    def forget(self):
        with self.lock:
            self.cache.pop(self.key, None)

    def _resolve(self):
        name = (self.conn.get("profile") or "").strip()
        if not name and not self.conn.get("start_url") and os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
            return {"access_key": os.environ["AWS_ACCESS_KEY_ID"], "secret_key": os.environ["AWS_SECRET_ACCESS_KEY"],
                    "session_token": os.environ.get("AWS_SESSION_TOKEN", ""), "expires": 0, "who": "environment variables"}
        sso = sso_settings(self.conn)
        if sso:
            if not (sso["account_id"] and sso["role_name"]):
                raise AwsAuthError("The SSO profile needs an account ID and a role name (sso_account_id and sso_role_name).")
            token = self.sso.token(sso)
            q = urllib.parse.urlencode({"account_id": sso["account_id"], "role_name": sso["role_name"]})
            try:
                r = net.get_json(_url("portal", sso["region"]) + "/federation/credentials?" + q,
                                 headers={"x-amz-sso_bearer_token": token}, timeout=20)
            except net.HTTPError as e:
                if e.status in (401, 403):
                    raise AwsAuthError("AWS SSO turned the sign-in down (%s). Sign in again, and check the account and role." % e) from None
                raise
            rc = r.get("roleCredentials") or {}
            return {"access_key": rc["accessKeyId"], "secret_key": rc["secretAccessKey"], "session_token": rc.get("sessionToken", ""),
                    "expires": _parse_time(rc.get("expiration") or 0), "who": "%s / %s (SSO)" % (sso["account_id"], sso["role_name"])}
        name = name or os.environ.get("AWS_PROFILE") or "default"
        prof = _section(_ini(config_path()), "default" if name == "default" else "profile " + name)
        if prof.get("credential_process"):
            out = subprocess.run(prof["credential_process"], shell=True, capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise AwsAuthError("The profile's credential_process failed: %s" % (out.stderr.strip() or out.stdout.strip())[:300])
            j = json.loads(out.stdout)
            return {"access_key": j["AccessKeyId"], "secret_key": j["SecretAccessKey"], "session_token": j.get("SessionToken", ""),
                    "expires": _parse_time(j.get("Expiration")) if j.get("Expiration") else 0, "who": "profile %s" % name}
        keys = _section(_ini(credentials_path()), name)
        if keys.get("aws_access_key_id") and keys.get("aws_secret_access_key"):
            return {"access_key": keys["aws_access_key_id"], "secret_key": keys["aws_secret_access_key"],
                    "session_token": keys.get("aws_session_token", ""), "expires": 0, "who": "profile %s" % name}
        raise AwsAuthError("No AWS sign-in found for profile \"%s\". Pick your SSO profile in Settings › Servers, or fill in "
                           "your SSO start URL, account and role there and press Sign in." % name)


# ── the event stream (application/vnd.amazon.eventstream) ────────────────
def _headers(raw):
    out, i = {}, 0
    while i < len(raw):
        n = raw[i]
        name = raw[i + 1:i + 1 + n].decode()
        i += 1 + n
        t = raw[i]
        i += 1
        if t in (0, 1):
            v = t == 0
        elif t == 2:
            v, i = raw[i], i + 1
        elif t == 3:
            (v,), i = struct.unpack(">h", raw[i:i + 2]), i + 2
        elif t == 4:
            (v,), i = struct.unpack(">i", raw[i:i + 4]), i + 4
        elif t in (5, 8):
            (v,), i = struct.unpack(">q", raw[i:i + 8]), i + 8
        elif t in (6, 7):
            (ln,) = struct.unpack(">H", raw[i:i + 2])
            v = raw[i + 2:i + 2 + ln]
            v = v.decode() if t == 7 else v
            i += 2 + ln
        elif t == 9:
            v, i = raw[i:i + 16], i + 16
        else:
            raise ValueError("Unknown event-stream header type %d" % t)
        out[name] = v
    return out


def iter_events(resp, cancel=None):
    """(headers, payload) for each message in an AWS event stream."""
    buf = b""
    read = getattr(resp, "read1", None) or resp.read
    while True:
        if cancel is not None and cancel.is_set():
            return
        chunk = read(65536)
        if not chunk:
            return
        buf += chunk
        while len(buf) >= 12:
            total, hlen, pcrc = struct.unpack(">III", buf[:12])
            if zlib.crc32(buf[:8]) & 0xffffffff != pcrc:
                raise ValueError("The response from Bedrock was garbled (event-stream prelude checksum).")
            if len(buf) < total:
                break
            msg, buf = buf[:total], buf[total:]
            yield _headers(msg[12:12 + hlen]), msg[12 + hlen:total - 4]


def encode_event(headers, payload):
    """One event-stream message (used by the tests' fake Bedrock)."""
    h = b""
    for k, v in headers.items():
        kb, vb = k.encode(), v.encode()
        h += bytes([len(kb)]) + kb + b"\x07" + struct.pack(">H", len(vb)) + vb
    total = 16 + len(h) + len(payload)
    prelude = struct.pack(">II", total, len(h))
    prelude += struct.pack(">I", zlib.crc32(prelude) & 0xffffffff)
    body = prelude + h + payload
    return body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)


# ── the provider ─────────────────────────────────────────────────────────
def _image_format(b64):
    head = base64.b64decode(b64[:24] + "=" * (-len(b64[:24]) % 4))
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head.startswith(b"GIF8"):
        return "gif"
    if head[8:12] == b"WEBP":
        return "webp"
    return "png"


class Bedrock(Base):
    caps = {"pull": False, "remove": False, "running": False, "show": False, "load": False}
    data_dir = os.path.join(os.path.expanduser("~"), ".vscode-assistant")      # set by the app at start

    def __init__(self, conn):
        conn = dict(conn, url=conn.get("url") or _url("runtime", conn.get("region") or "us-east-1"))
        super().__init__(conn)
        self.region = (conn.get("region") or "us-east-1").strip()
        self.creds = Credentials(conn, self.data_dir)

    def _send(self, kind, method, path, body=None, timeout=60, service="bedrock"):
        url = _url(kind, self.region) + path
        payload = json.dumps(body).encode() if body is not None else b""
        hdrs = [("Content-Type", "application/json")] if body is not None else []
        if kind == "runtime":
            hdrs.append(("Accept", "application/vnd.amazon.eventstream" if path.endswith("-stream") else "application/json"))
        for attempt in (0, 1):
            c = self.creds.get()
            signed = sigv4(method, url, hdrs, payload, dict(c, region=self.region, service=service))
            try:
                return net.open_url(url, method, payload if body is not None else None, dict(signed), timeout=timeout)
            except net.HTTPError as e:
                text = str(e)
                if attempt == 0 and e.status in (400, 403) and ("expired" in text.lower() or "ExpiredToken" in text):
                    self.creds.forget()            # the temporary keys ran out: fetch new ones once
                    continue
                if e.status == 403 and "not authorized" in text.lower():
                    raise RuntimeError("AWS says your role isn't allowed to do this (%s). Ask your AWS admin for "
                                       "bedrock:InvokeModel and bedrock:InvokeModelWithResponseStream on this model." % text) from None
                if e.status == 403 and "access to the model" in text.lower():
                    raise RuntimeError("This model isn't enabled for your AWS account in %s yet (%s). Your AWS admin "
                                       "turns it on under Bedrock › Model access." % (self.region, text)) from None
                raise

    def ping(self):
        models = self.list_models()
        return "Amazon Bedrock · %s · %d model%s · signed in as %s" % (self.region, len(models), "" if len(models) == 1 else "s",
                                                                      self.creds.get().get("who", "?"))

    def list_models(self):
        """Inference profiles (how newer Claude models are called, e.g. us.anthropic.…) and on-demand models, plus any typed in."""
        seen, out, errors = set(), [], []
        for mid in (self.conn.get("models") or "").replace(",", "\n").split("\n"):
            mid = mid.strip()
            if mid and mid not in seen:
                seen.add(mid)
                out.append({"name": mid, "owner": "typed in"})
        try:
            with self._send("control", "GET", "/inference-profiles?maxResults=1000", timeout=20) as r:
                for p in json.loads(r.read() or b"{}").get("inferenceProfileSummaries") or []:
                    if p.get("status", "ACTIVE") == "ACTIVE" and p.get("inferenceProfileId") not in seen:
                        seen.add(p["inferenceProfileId"])
                        out.append({"name": p["inferenceProfileId"], "owner": p.get("inferenceProfileName") or "inference profile"})
        except (net.HTTPError, RuntimeError) as e:
            errors.append(str(e))
        try:
            with self._send("control", "GET", "/foundation-models?byOutputModality=TEXT", timeout=20) as r:
                for m in json.loads(r.read() or b"{}").get("modelSummaries") or []:
                    if "ON_DEMAND" in (m.get("inferenceTypesSupported") or []) and m["modelId"] not in seen and \
                            (m.get("modelLifecycle") or {}).get("status", "ACTIVE") == "ACTIVE":
                        seen.add(m["modelId"])
                        out.append({"name": m["modelId"], "owner": m.get("providerName") or ""})
        except (net.HTTPError, RuntimeError) as e:
            errors.append(str(e))
        if not out and errors:
            raise RuntimeError("Couldn't list the Bedrock models (%s). If your role can't list them, type the model IDs "
                               "into the server's Models box, e.g. the inference profile ID shown in the Bedrock console." % errors[0])
        claude = lambda m: (0 if "anthropic" in m["name"] else 1, 0 if "sonnet" in m["name"] else 1, m["name"])
        return sorted(out, key=claude)

    def _wire(self, messages):
        system, out = [], []

        def add(role, blocks):
            if not blocks:
                return
            if out and out[-1]["role"] == role:          # Converse wants user and assistant to take turns
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        for m in messages:
            role, text = m["role"], m.get("content") or ""
            if role == "system":
                if text.strip():
                    system.append({"text": text})
            elif role == "user":
                blocks = [{"text": text}] if text.strip() else []
                blocks += [{"image": {"format": _image_format(b), "source": {"bytes": b}}} for b in m.get("images") or []]
                add("user", blocks or [{"text": "(empty message)"}])
            elif role == "assistant":
                text = _strip_think(text)
                blocks = [{"text": text}] if text.strip() else []
                blocks += [{"toolUse": {"toolUseId": c["id"], "name": c["name"], "input": c.get("arguments") or {}}}
                           for c in m.get("tool_calls") or []]
                add("assistant", blocks)
            elif role == "tool":
                add("user", [{"toolResult": {"toolUseId": m.get("tool_call_id", ""), "content": [{"text": text or "(no output)"}]}}])
        while out and out[0]["role"] != "user":         # it must start with the person
            out.pop(0)
        return system, out

    def chat(self, model, messages, params, on_delta, cancel, tools=None, keep_alive=None):
        system, msgs = self._wire(messages)
        docs = params.get("documents") or []
        if docs and msgs:                 # PDF files the model reads itself: with the first thing the person said
            used, blocks = set(), []
            for d in docs:
                name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9\s\-()\[\]]", " ", os.path.splitext(d["name"])[0])).strip() or "Document"
                base, n = name[:180], 2
                while name in used:
                    name, n = "%s (%d)" % (base, n), n + 1
                used.add(name)
                blocks.append({"document": {"format": "pdf", "name": name, "source": {"bytes": base64.b64encode(d["data"]).decode()}}})
            msgs[0]["content"] = blocks + msgs[0]["content"]
        body = {"messages": msgs, "inferenceConfig": {"maxTokens": int(params.get("max_tokens") or 8192)}}
        if system:
            body["system"] = system
        if params.get("temperature") is not None and params.get("temperature") != "":
            body["inferenceConfig"]["temperature"] = float(params["temperature"])
        if params.get("top_p"):
            body["inferenceConfig"]["topP"] = float(params["top_p"])
        if tools:
            body["toolConfig"] = {"tools": [{"toolSpec": {"name": t["function"]["name"], "description": t["function"].get("description") or "",
                                                          "inputSchema": {"json": t["function"].get("parameters") or {"type": "object", "properties": {}}}}}
                                            for t in tools]}
        t0 = time.time()
        first, chunks, usage = 0, 0, None
        calls, current = [], None
        path = "/model/%s/converse-stream" % urllib.parse.quote(model, safe="")
        with self._send("runtime", "POST", path, body, timeout=600) as r:
            for headers, payload in iter_events(r, cancel):
                kind = headers.get(":event-type") or headers.get(":exception-type") or ""
                j = json.loads(payload or b"{}")
                if headers.get(":message-type") == "exception" or kind.endswith("Exception"):
                    raise RuntimeError("Bedrock: %s%s" % (kind, ": " + j.get("message", "") if j.get("message") else ""))
                if kind == "contentBlockStart":
                    tu = (j.get("start") or {}).get("toolUse")
                    if tu:
                        current = {"id": tu.get("toolUseId") or uuid.uuid4().hex[:12], "name": tu.get("name", ""), "args": ""}
                        calls.append(current)
                elif kind == "contentBlockDelta":
                    d = j.get("delta") or {}
                    if "text" in d or "reasoningContent" in d:
                        text, think = d.get("text") or "", (d.get("reasoningContent") or {}).get("text") or ""
                        if text or think:
                            first = first or time.time()
                            chunks += 1
                            on_delta(text, think)
                    if "toolUse" in d and current is not None:
                        current["args"] += (d["toolUse"] or {}).get("input") or ""
                elif kind == "contentBlockStop":
                    current = None
                elif kind == "metadata":
                    usage = j.get("usage") or usage
        out = []
        for c in calls:
            try:
                args = json.loads(c["args"]) if c["args"].strip() else {}
            except ValueError:
                args = {"_raw": c["args"]}
            out.append({"id": c["id"], "name": c["name"], "arguments": args})
        end = time.time()
        tokens = (usage or {}).get("outputTokens") or chunks
        return out, {"tokens": tokens, "approx": not usage,
                     "tps": tokens / (end - first) if first and end > first else None, "seconds": end - t0}
