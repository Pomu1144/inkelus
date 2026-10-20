"""Clients for local model servers.

ollama  native API: list / running / show / pull / delete / load / unload / chat
openai  any OpenAI-compatible server (LM Studio, llama.cpp server, vLLM,
        LocalAI, Jan…): list models and chat

Both support tool calling, which is how the sandbox web tools reach the model.

Messages use one internal shape:
  {"role": "system"|"user"|"assistant"|"tool", "content": str,
   "images": [base64...],                       # user, optional
   "tool_calls": [{"id", "name", "arguments": dict}],   # assistant, optional
   "tool_call_id": str, "name": str}            # tool results
"""
import json
import time
import uuid

from . import net


def _strip_think(text):
    """Drop a leading <think>…</think> block before sending history back."""
    t = text.lstrip()
    if t.startswith("<think>"):
        end = t.find("</think>")
        return t[end + 8:].lstrip() if end >= 0 else ""
    return text


class Base:
    caps = {}

    def __init__(self, conn):
        self.conn = conn
        self.base = conn["url"].strip().rstrip("/")

    def headers(self):
        h = {}
        if self.conn.get("key"):
            h["Authorization"] = "Bearer " + self.conn["key"]
        return h


class Ollama(Base):
    caps = {"pull": True, "remove": True, "running": True, "show": True, "load": True}

    def ping(self):
        v = net.get_json(self.base + "/api/version", headers=self.headers(), timeout=5)
        return "Ollama" + (" v" + v["version"] if v.get("version") else "")

    def list_models(self):
        r = net.get_json(self.base + "/api/tags", headers=self.headers(), timeout=10)
        out = []
        for m in r.get("models", []):
            d = m.get("details") or {}
            out.append({"name": m.get("name") or m.get("model"), "size": m.get("size"),
                        "modified": m.get("modified_at"), "family": d.get("family"),
                        "params": d.get("parameter_size"), "quant": d.get("quantization_level")})
        return sorted(out, key=lambda m: m["name"])

    def running(self):
        r = net.get_json(self.base + "/api/ps", headers=self.headers(), timeout=10)
        out = []
        for m in r.get("models", []):
            d = m.get("details") or {}
            out.append({"name": m.get("name") or m.get("model"), "size": m.get("size"),
                        "vram": m.get("size_vram"), "expires": m.get("expires_at"),
                        "params": d.get("parameter_size"), "ctx": m.get("context_length")})
        return out

    def show(self, name):
        return net.get_json(self.base + "/api/show", "POST", {"model": name}, self.headers())

    def remove(self, name):
        net.get_json(self.base + "/api/delete", "DELETE", {"model": name}, self.headers())

    def load(self, name, keep_alive=None):
        net.get_json(self.base + "/api/generate", "POST", {"model": name, "keep_alive": keep_alive or "5m"},
                     self.headers(), timeout=600)

    def unload(self, name):
        net.get_json(self.base + "/api/generate", "POST", {"model": name, "keep_alive": 0}, self.headers())

    def pull(self, name, on_progress, cancel):
        r = net.open_url(self.base + "/api/pull", "POST", {"model": name, "stream": True}, self.headers(), timeout=600)
        with r:
            for line in net.iter_lines(r, cancel):
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("error"):
                    raise RuntimeError(j["error"])
                on_progress(j)

    def _wire(self, messages):
        out = []
        for m in messages:
            o = {"role": m["role"], "content": m.get("content") or ""}
            if m["role"] == "assistant":
                o["content"] = _strip_think(o["content"])
            if m.get("images"):
                o["images"] = m["images"]
            if m.get("tool_calls"):
                o["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
            if m["role"] == "tool":
                o["tool_name"] = m.get("name", "")
            out.append(o)
        return out

    def chat(self, model, messages, params, on_delta, cancel, tools=None, keep_alive=None):
        options = {}
        if params.get("temperature") is not None:
            options["temperature"] = params["temperature"]
        if params.get("top_p") is not None:
            options["top_p"] = params["top_p"]
        if params.get("num_ctx"):
            options["num_ctx"] = params["num_ctx"]
        if params.get("max_tokens"):
            options["num_predict"] = params["max_tokens"]
        body = {"model": model, "stream": True, "options": options, "messages": self._wire(messages)}
        if tools:
            body["tools"] = tools
        if keep_alive:
            body["keep_alive"] = keep_alive
        calls, stats = [], {}
        r = net.open_url(self.base + "/api/chat", "POST", body, self.headers(), timeout=600)
        with r:
            for line in net.iter_lines(r, cancel):
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("error"):
                    raise RuntimeError(j["error"])
                msg = j.get("message") or {}
                if msg.get("content") or msg.get("thinking"):
                    on_delta(msg.get("content") or "", msg.get("thinking") or "")
                for c in msg.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"_raw": args}
                    calls.append({"id": c.get("id") or uuid.uuid4().hex[:12], "name": fn.get("name", ""), "arguments": args})
                if j.get("done"):
                    ec, ed = j.get("eval_count"), j.get("eval_duration")
                    stats = {"tokens": ec, "tps": ec / (ed / 1e9) if ec and ed else None,
                             "seconds": (j.get("total_duration") or 0) / 1e9 or None}
        return calls, stats


class OpenAICompat(Base):
    caps = {"pull": False, "remove": False, "running": False, "show": False, "load": False}

    def p(self, path):
        return self.base + ("" if self.base.endswith("/v1") else "/v1") + path

    def ping(self):
        n = len(self.list_models())
        return "OpenAI-compatible · %d model%s" % (n, "" if n == 1 else "s")

    def list_models(self):
        r = net.get_json(self.p("/models"), headers=self.headers(), timeout=10)
        return sorted(({"name": m.get("id") or m.get("name"), "owner": m.get("owned_by")}
                       for m in r.get("data") or r.get("models") or []), key=lambda m: m["name"])

    def _wire(self, messages):
        out = []
        for m in messages:
            content = m.get("content") or ""
            if m["role"] == "assistant":
                content = _strip_think(content)
            if m.get("images"):
                content = [{"type": "text", "text": content}] + [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}} for b in m["images"]]
            o = {"role": m["role"], "content": content}
            if m.get("tool_calls"):
                o["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                   for c in m["tool_calls"]]
                if not content:
                    o["content"] = None
            if m["role"] == "tool":
                o["tool_call_id"] = m.get("tool_call_id", "")
            out.append(o)
        return out

    def chat(self, model, messages, params, on_delta, cancel, tools=None, keep_alive=None):
        body = {"model": model, "stream": True, "messages": self._wire(messages)}
        for k in ("temperature", "top_p", "max_tokens"):
            if params.get(k):
                body[k] = params[k]
        if params.get("temperature") == 0:
            body["temperature"] = 0
        if tools:
            body["tools"] = tools
        t0 = time.time()
        first, chunks, usage = 0, 0, None
        acc = {}  # tool-call deltas by index
        r = net.open_url(self.p("/chat/completions"), "POST", body, self.headers(), timeout=600)
        with r:
            for line in net.iter_lines(r, cancel):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except ValueError:
                    continue
                if j.get("error"):
                    e = j["error"]
                    raise RuntimeError(e.get("message") if isinstance(e, dict) else e)
                usage = j.get("usage") or usage
                ch = (j.get("choices") or [{}])[0]
                d = ch.get("delta") or {}
                text = d.get("content") or ""
                think = d.get("reasoning_content") or d.get("reasoning") or ""
                if text or think:
                    first = first or time.time()
                    chunks += 1
                    on_delta(text, think)
                for tc in d.get("tool_calls") or []:
                    slot = acc.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
        calls = []
        for i in sorted(acc):
            s = acc[i]
            try:
                args = json.loads(s["args"]) if s["args"].strip() else {}
            except ValueError:
                args = {"_raw": s["args"]}
            calls.append({"id": s["id"] or uuid.uuid4().hex[:12], "name": s["name"], "arguments": args})
        end = time.time()
        tokens = (usage or {}).get("completion_tokens") or chunks
        return calls, {"tokens": tokens, "approx": not usage,
                       "tps": tokens / (end - first) if first and end > first else None, "seconds": end - t0}


def create(conn):
    return OpenAICompat(conn) if conn.get("type") == "openai" else Ollama(conn)
