"""The Java debugger: breakpoints, stepping, the call stack, variables and a console, like NetBeans'.

It uses the java-debug plugin that runs inside the JDT language server (javals.py): the server starts a
debug adapter on a local port, and this module speaks the Debug Adapter Protocol to it. A program is
either launched by the adapter (Debug on a main method) or started by a build tool waiting for a debugger
(Debug on a test: Maven Surefire, Gradle or NetBeans Ant with a JDWP agent) and then attached to.
Everything the page needs arrives as {"type": "debug", ...} events.
"""
import os
import socket
import threading

from .javals import read_message, uri_path, write_message


class DebugError(Exception):
    pass


class Session:
    def __init__(self, server, project_id, publish):
        self.server, self.id, self.publish = server, project_id, publish
        self.sock = self.rfile = self.wfile = None
        self.seq, self.lock, self.pending = 1, threading.Lock(), {}
        self.state = "starting"          # starting, running, stopped (at a breakpoint), ended
        self.thread_id, self.frames = None, []
        self.breakpoints = {}            # absolute path -> [{"line", "condition"}]
        self.initialized = threading.Event()
        self.label = ""

    # -- the connection --
    def _connect(self, port):
        self.sock = socket.create_connection(("127.0.0.1", int(port)), timeout=30)
        self.sock.settimeout(None)
        self.rfile, self.wfile = self.sock.makefile("rb"), self.sock.makefile("wb")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                msg = read_message(self.rfile)
                if msg is None:
                    break
                t = msg.get("type")
                if t == "response" and msg.get("request_seq") in self.pending:
                    box = self.pending.pop(msg["request_seq"])
                    box["msg"] = msg
                    box["event"].set()
                elif t == "event":
                    self._event(msg.get("event"), msg.get("body") or {})
                elif t == "request":       # reverse requests (runInTerminal): we don't offer them
                    self._send({"type": "response", "request_seq": msg.get("seq"), "command": msg.get("command"), "success": False})
        except (OSError, ValueError):
            pass
        finally:
            for box in list(self.pending.values()):
                box["event"].set()
            self._end()

    def _send(self, msg):
        with self.lock:
            msg["seq"] = self.seq
            self.seq += 1
        write_message(self.wfile, threading.Lock(), msg)
        return msg["seq"]

    def request(self, command, args=None, timeout=30):
        if self.state == "ended" or not self.wfile:
            raise DebugError("The debug session has ended.")
        box = {"event": threading.Event(), "msg": None}
        with self.lock:
            seq = self.seq
            self.pending[seq] = box
        try:
            self._send({"type": "request", "command": command, "arguments": args or {}})
        except OSError:
            self.pending.pop(seq, None)
            raise DebugError("The debug session has ended.")
        if not box["event"].wait(timeout):
            self.pending.pop(seq, None)
            raise DebugError("The debugger didn't answer (%s)." % command)
        msg = box["msg"]
        if msg is None:
            raise DebugError("The debug session has ended.")
        if not msg.get("success"):
            raise DebugError(msg.get("message") or ((msg.get("body") or {}).get("error") or {}).get("format") or "%s failed" % command)
        return msg.get("body") or {}

    def _emit(self, kind, **kw):
        self.publish(dict({"type": "debug", "project": self.id, "kind": kind}, **kw))

    # -- events from the adapter --
    def _event(self, name, body):
        if name == "initialized":
            self.initialized.set()
        elif name == "output":
            text = body.get("output") or ""
            if text and body.get("category") != "telemetry":
                self._emit("output", category=body.get("category") or "console", text=text)
        elif name == "stopped":
            self.state = "stopped"
            self.thread_id = body.get("threadId") or self.thread_id
            threading.Thread(target=self._stopped, args=(body.get("reason") or "", body.get("description") or "", body.get("text") or ""), daemon=True).start()
        elif name == "continued":
            self.state = "running"
            self._emit("state", state="running")
        elif name in ("terminated", "exited"):
            if name == "exited":
                self._emit("output", category="console", text="\nThe program exited with code %s.\n" % body.get("exitCode"))
            self._end()

    def _stopped(self, reason, description, text):
        try:
            st = self.request("stackTrace", {"threadId": self.thread_id, "startFrame": 0, "levels": 40})
            self.frames = [self._frame(f) for f in st.get("stackFrames") or []]
            scopes = self.scopes(self.frames[0]["id"]) if self.frames else []
            threads = [{"id": t.get("id"), "name": t.get("name")} for t in (self.request("threads").get("threads") or [])]
        except DebugError as e:
            self._emit("output", category="stderr", text="Couldn't read where the program stopped: %s\n" % e)
            return
        self._emit("stopped", reason=reason, description=description or reason, text=text, thread=self.thread_id,
                   threads=threads, frames=self.frames, scopes=scopes)

    def _frame(self, f):
        src = f.get("source") or {}
        path = src.get("path") or ""
        rel = None
        if path:
            p = uri_path(path) if path.startswith("file:") else os.path.normpath(path)
            root = self.server.root
            if p and os.path.realpath(p).startswith(root + os.sep):
                rel = os.path.relpath(os.path.realpath(p), root).replace(os.sep, "/")
        return {"id": f.get("id"), "name": f.get("name") or "", "line": f.get("line") or 0, "column": f.get("column") or 0,
                "path": rel, "source": src.get("name") or os.path.basename(path), "ref": src.get("sourceReference") or 0,
                "uri": path if path.startswith("jdt:") else ""}

    def scopes(self, frame_id):
        out = []
        for s in self.request("scopes", {"frameId": frame_id}).get("scopes") or []:
            out.append({"name": s.get("name"), "ref": s.get("variablesReference"),
                        "vars": self.variables(s.get("variablesReference")) if s.get("variablesReference") and not s.get("expensive") else []})
        return out

    def variables(self, ref):
        out = []
        for v in (self.request("variables", {"variablesReference": ref}).get("variables") or [])[:300]:
            out.append({"name": v.get("name"), "value": v.get("value"), "type": v.get("type") or "", "ref": v.get("variablesReference") or 0})
        return out

    def _end(self):
        if self.state == "ended":
            return
        self.state = "ended"
        self._emit("ended")
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    # -- starting --
    def _handshake(self):
        self.request("initialize", {"clientID": "vscode-assistant", "clientName": "VSCode Assistant", "adapterID": "java",
                                    "pathFormat": "path", "linesStartAt1": True, "columnsStartAt1": True,
                                    "supportsVariableType": True, "supportsVariablePaging": False, "supportsRunInTerminalRequest": False,
                                    "locale": "en"})

    def _configure(self, breakpoints):
        if not self.initialized.wait(60):
            raise DebugError("The debugger didn't get ready.")
        for path, lines in (breakpoints or {}).items():
            self.set_breakpoints(path, lines)
        try:
            self.request("setExceptionBreakpoints", {"filters": []})
        except DebugError:
            pass
        self.request("configurationDone")
        self.state = "running"
        self._emit("state", state="running", label=self.label)

    def launch(self, main_class, args="", vm_args="", breakpoints=None):
        """Start main_class under the debugger (runs on a worker thread; reports through events)."""
        self.label = main_class
        project = self._project_name(main_class)
        cp = self.server.execute("vscode.java.resolveClasspath", [main_class, project], timeout=120) or [[], []]
        port = self.server.execute("vscode.java.startDebugSession", [], timeout=60)
        self._connect(port)
        self._handshake()
        resp = {}

        def do_launch():
            try:
                resp["body"] = self.request("launch", {
                    "type": "java", "request": "launch", "name": "Debug " + main_class, "mainClass": main_class, "projectName": project,
                    "modulePaths": cp[0] or [], "classPaths": cp[1] or [], "cwd": self.server.root, "args": args or "", "vmArgs": vm_args or "",
                    "console": "internalConsole", "stopOnEntry": False, "encoding": "UTF-8", "shortenCommandLine": "auto"}, timeout=120)
            except DebugError as e:
                resp["error"] = e
                self.initialized.set()
        t = threading.Thread(target=do_launch, daemon=True)
        t.start()
        self._configure(breakpoints)
        t.join(120)
        if resp.get("error"):
            raise resp["error"]

    def attach(self, port, label, breakpoints=None):
        """Attach to a program waiting for a debugger on a local port (a test started by Maven, Gradle or Ant)."""
        self.label = label
        dport = self.server.execute("vscode.java.startDebugSession", [], timeout=60)
        self._connect(dport)
        self._handshake()
        resp = {}

        def do_attach():
            try:
                resp["body"] = self.request("attach", {"type": "java", "request": "attach", "name": label, "hostName": "localhost",
                                                       "port": int(port), "timeout": 30000}, timeout=60)
            except DebugError as e:
                resp["error"] = e
                self.initialized.set()
        t = threading.Thread(target=do_attach, daemon=True)
        t.start()
        self._configure(breakpoints)
        t.join(60)
        if resp.get("error"):
            raise resp["error"]

    def _project_name(self, main_class):
        try:
            for m in self.server.execute("vscode.java.resolveMainClass", [self.server.info()["root_uri"]], timeout=60) or []:
                if m.get("mainClass") == main_class:
                    return m.get("projectName") or ""
        except Exception:  # noqa: BLE001 - the project name is optional
            pass
        return ""

    # -- while it runs --
    def set_breakpoints(self, path, lines):
        """lines: [{"line": 12, "condition": ""}] for one file (absolute path); replaces that file's breakpoints."""
        self.breakpoints[path] = lines
        if self.state == "ended" or not self.wfile:
            return []
        body = self.request("setBreakpoints", {"source": {"path": path, "name": os.path.basename(path)},
                                               "breakpoints": [dict({"line": int(b["line"])}, **({"condition": b["condition"]} if b.get("condition") else {}))
                                                               for b in lines],
                                               "lines": [int(b["line"]) for b in lines], "sourceModified": False})
        return [{"line": b.get("line"), "verified": b.get("verified", False), "message": b.get("message") or ""} for b in body.get("breakpoints") or []]

    def control(self, op, thread=None):
        tid = thread or self.thread_id
        if op == "continue":
            self.request("continue", {"threadId": tid})
        elif op in ("next", "stepIn", "stepOut"):
            self.request(op, {"threadId": tid})
        elif op == "pause":
            self.request("pause", {"threadId": tid or 0})
        elif op == "stop":
            try:
                self.request("disconnect", {"restart": False, "terminateDebuggee": True}, timeout=10)
            except DebugError:
                pass
            self._end()
            return
        if op != "pause":
            self.state = "running"
            self._emit("state", state="running")

    def frame(self, frame_id):
        return {"scopes": self.scopes(frame_id)}

    def evaluate(self, expression, frame_id):
        body = self.request("evaluate", {"expression": expression, "frameId": frame_id, "context": "repl"}, timeout=30)
        return {"value": body.get("result"), "type": body.get("type") or "", "ref": body.get("variablesReference") or 0}

    def source(self, ref):
        return self.request("source", {"sourceReference": int(ref)}).get("content") or ""


def port_open(port):
    """Whether a program is listening on a local port (a JVM waiting for a debugger accepts again after this)."""
    try:
        socket.create_connection(("127.0.0.1", int(port)), timeout=1).close()
        return True
    except OSError:
        return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
