"""WebSocket and Server-Sent Events connections for the API tab.

The app's server holds the connection (so custom headers, wss:// and self-signed local certificates
work) and streams each message to the page as an event; messages typed in the page are sent on.
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request

from .store import new_id

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSClient:
    """A WebSocket client (RFC 6455) with TLS, headers and subprotocols."""

    def __init__(self, url, headers=(), protocols="", timeout=15, verify=True):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError("A WebSocket URL starts with ws:// or wss://")
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        host = u.hostname + ("" if u.port is None else ":%d" % u.port)
        lines = ["GET %s HTTP/1.1" % path, "Host: " + host, "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
        if protocols:
            lines.append("Sec-WebSocket-Protocol: " + protocols)
        names = {k.lower() for k, _ in headers}
        if "origin" not in names:
            lines.append("Origin: %s://%s" % ("https" if u.scheme == "wss" else "http", host))
        lines += ["%s: %s" % (k, v) for k, v in headers]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("The server closed the connection during the handshake.")
            head += chunk
            if len(head) > 65536:
                raise ConnectionError("The handshake response was too long.")
        head, self._buf = head.split(b"\r\n\r\n", 1)
        resp = head.decode("latin-1").split("\r\n")
        self.status_line = resp[0]
        self.response_headers = [tuple(x.split(":", 1)) for x in resp[1:] if ":" in x]
        self.response_headers = [(k.strip(), v.strip()) for k, v in self.response_headers]
        if " 101 " not in resp[0] + " ":
            raise ConnectionError("The server refused the WebSocket upgrade: " + resp[0])
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        accept = next((v for k, v in self.response_headers if k.lower() == "sec-websocket-accept"), "")
        if accept != want:
            raise ConnectionError("Bad WebSocket handshake (Sec-WebSocket-Accept didn't match).")
        sock.settimeout(None)
        self._lock = threading.Lock()
        self.closed = False

    def _frame(self, opcode, payload):
        head = bytes([0x80 | opcode])
        n = len(payload)
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._lock:
            self.sock.sendall(head + mask + body)

    def send(self, text, binary=False):
        self._frame(0x2 if binary else 0x1, text if isinstance(text, bytes) else text.encode())

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("The connection was closed.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """(kind, data): ("text", str), ("binary", bytes) or ("close", "code reason")."""
        parts, kind = [], None
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                try:
                    self._frame(0x8, data[:2])
                except OSError:
                    pass
                return "close", "%d %s" % (code, data[2:].decode("utf-8", "replace"))
            if opcode == 0x9:
                self._frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind = "text" if opcode == 0x1 else "binary"
            parts.append(data)
            if fin:
                raw = b"".join(parts)
                return (kind or "text"), raw.decode("utf-8", "replace") if kind != "binary" else raw

    def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            self._frame(0x8, struct.pack(">H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Sessions:
    """Open WebSocket / SSE connections, each streaming to the page as {"type": "rt", ...} events."""

    def __init__(self, publish):
        self.publish = publish
        self.items = {}

    def _emit(self, sid, direction, data, kind="text"):
        self.publish({"type": "rt", "id": sid, "dir": direction, "kind": kind, "data": data, "ts": time.time()})

    def open(self, kind, url, headers=(), protocols="", verify=True):
        sid = new_id()
        if kind == "ws":
            ws = WSClient(url, headers, protocols, verify=verify)
            self.items[sid] = {"kind": "ws", "conn": ws, "url": url}

            def pump():
                try:
                    while True:
                        k, data = ws.recv()
                        if k == "close":
                            if not ws.closed:
                                self._emit(sid, "sys", "Closed by the server: " + data)
                            break
                        if k == "binary":
                            self._emit(sid, "in", base64.b64encode(data).decode(), "binary")
                        else:
                            self._emit(sid, "in", data)
                except (OSError, ConnectionError) as e:
                    if not ws.closed:
                        self._emit(sid, "sys", "Disconnected: %s" % e)
                finally:
                    ws.close()
                    if self.items.pop(sid, None) is not None:
                        self.publish({"type": "rt_closed", "id": sid})
            threading.Thread(target=pump, daemon=True).start()
            return {"id": sid, "status": ws.status_line, "headers": ws.response_headers}
        if kind == "sse":
            req = urllib.request.Request(url, headers=dict([("Accept", "text/event-stream"), ("Cache-Control", "no-cache")] + list(headers)))
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            handlers = [urllib.request.HTTPSHandler(context=ctx)]
            if host in ("localhost", "127.0.0.1", "::1"):
                handlers.append(urllib.request.ProxyHandler({}))
            resp = urllib.request.build_opener(*handlers).open(req, timeout=30)
            item = self.items[sid] = {"kind": "sse", "conn": resp, "url": url, "closed": False}

            def pump():
                event, data, last_id = "message", [], ""
                try:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if data:
                                self.publish({"type": "rt", "id": sid, "dir": "in", "kind": "text", "event": event,
                                              "event_id": last_id, "data": "\n".join(data), "ts": time.time()})
                            event, data = "message", []
                            continue
                        if line.startswith(":"):
                            continue
                        field, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if field == "event":
                            event = value or "message"
                        elif field == "data":
                            data.append(value)
                        elif field == "id":
                            last_id = value
                    self._emit(sid, "sys", "The server ended the stream.")
                except (OSError, ValueError) as e:
                    if not item["closed"]:
                        self._emit(sid, "sys", "Disconnected: %s" % e)
                finally:
                    if self.items.pop(sid, None) is not None:
                        self.publish({"type": "rt_closed", "id": sid})
            threading.Thread(target=pump, daemon=True).start()
            return {"id": sid, "status": "%s %s" % (resp.status, resp.reason), "headers": list(resp.headers.items())}
        raise ValueError("Unknown connection type.")

    def send(self, sid, text):
        it = self.items.get(sid)
        if not it or it["kind"] != "ws":
            raise ValueError("That connection is closed.")
        it["conn"].send(text)
        self._emit(sid, "out", text)

    def close(self, sid):
        it = self.items.pop(sid, None)
        if not it:
            return
        if it["kind"] == "ws":
            it["conn"].close()
        else:
            it["closed"] = True
            try:
                it["conn"].close()
            except OSError:
                pass
        self.publish({"type": "rt_closed", "id": sid})

    def close_all(self):
        for sid in list(self.items):
            self.close(sid)
