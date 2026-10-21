"""Projects: code folders on this computer that you and the AI work on together.

A project is a folder: one you already have (a cloned repo), one cloned here
from GitHub, or one unpacked from a ZIP. The app never copies or uploads it;
it reads and writes the files in place, so your own tools and Git see the
same thing.

The AI gets tools to look around (list, read, search), to change files (each
change is shown to you as a diff to approve first), to run commands such as
tests (you approve each one, or allow a command for the project once), and to
use Git. It can only reach files inside the project folder, and never inside
.git.
"""
import codecs
import difflib
import json
import fnmatch
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile

from . import api as apimod, gitops, library as libmod, tasks as taskmod, testlog
from .store import new_id

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
               ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt", "target", ".gradle",
               ".idea", ".DS_Store", "coverage", ".cache", "bower_components", ".terraform"}
MAX_FILES = 50000
MAX_READ_LINES = 800
MAX_TEXT = 2 * 1024 * 1024          # files bigger than this aren't opened as text
OUTPUT_LIMIT = 14000
CMD_TIMEOUT = 300
SHELL_CHAINING = re.compile(r"[;&|`<>\n]|\$\(")


class ToolError(Exception):
    pass


# ── files ────────────────────────────────────────────────────────────────
def read_text(full):
    """(text with \\n line ends, format) where format remembers BOM and CRLF so writes keep them."""
    try:
        size = os.path.getsize(full)
    except OSError:
        raise ToolError("No such file.")
    if size > MAX_TEXT:
        raise ToolError("That file is too big to open as text (%d MB)." % (size // (1024 * 1024)))
    with open(full, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        raise ToolError("That's a binary file, not text.")
    bom = data.startswith(codecs.BOM_UTF8)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError("That file isn't UTF-8 text, so it can't be edited here.")
    crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), {"bom": bom, "crlf": crlf}


def write_text(full, text, fmt=None):
    fmt = fmt or {}
    if fmt.get("crlf"):
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("utf-8")
    if fmt.get("bom"):
        data = codecs.BOM_UTF8 + data
    d = os.path.dirname(full)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(full, "wb") as f:    # in place, so permissions (e.g. the executable bit) are kept
        f.write(data)


def unified(rel, before, after):
    a = (before or "").splitlines(True)
    b = (after or "").splitlines(True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n\\ No newline at end of file\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n\\ No newline at end of file\n"
    return "".join(difflib.unified_diff(a, b, "/dev/null" if before is None else "a/" + rel,
                                        "/dev/null" if after is None else "b/" + rel, n=3))


def diff_counts(d):
    add = sum(1 for l in d.splitlines() if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in d.splitlines() if l.startswith("-") and not l.startswith("---"))
    return add, rem


def clip(text, limit=OUTPUT_LIMIT):
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + "\n…[%d characters cut]…\n" % (len(text) - limit) + text[-(limit - head):]


# ── commands ─────────────────────────────────────────────────────────────
def command_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("FORCE_COLOR", "0")
    env["NO_COLOR"] = "1"
    return env


def spawn(command, cwd):
    kw = dict(cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
              env=command_env())
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(command, **kw)


def kill_tree(p):
    if p.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            p.kill()
        except OSError:
            pass


def stream(p, on_text):
    """Read a process's output as it arrives (UTF-8, falling back to replacement characters)."""
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
        if not chunk:
            break
        on_text(dec.decode(chunk))
    tail = dec.decode(b"", final=True)
    if tail:
        on_text(tail)


def run_command(command, cwd, timeout=CMD_TIMEOUT, cancel=None):
    """Run a shell command to the end (or the timeout / cancel). Returns (exit code or None, output)."""
    p = spawn(command, cwd)
    out = []
    reader = threading.Thread(target=stream, args=(p, out.append), daemon=True)
    reader.start()
    end = time.time() + timeout
    code, why = None, ""
    while True:
        code = p.poll()
        if code is not None:
            break
        if cancel is not None and cancel.is_set():
            why = "stopped"
            break
        if time.time() > end:
            why = "timeout"
            break
        time.sleep(0.1)
    if code is None:
        kill_tree(p)
    reader.join(3)
    text = "".join(out).replace("\r\n", "\n")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)      # colour codes
    if why == "timeout":
        text += "\n[stopped after %d seconds]" % timeout
    elif why == "stopped":
        text += "\n[stopped by the user]"
    return code, text


def command_allowed(command, prefixes):
    """True if the user has allowed this command for the project (and it doesn't chain other commands)."""
    c = " ".join((command or "").split())
    if not c or SHELL_CHAINING.search(c):
        return False
    return any(p and (c == p or c.startswith(p + " ")) for p in prefixes or [])


def suggest_prefix(command):
    """The part of a command to offer for "always allow": e.g. "npm test", "pytest", "python -m pytest"."""
    words = (command or "").split()
    if not words:
        return ""
    n = 1
    if words[0] in ("npm", "pnpm", "yarn", "cargo", "go", "dotnet", "make", "mvn", "gradle", "./gradlew", "bun",
                    "poetry", "uv", "deno", "composer", "bundle", "mix", "flutter", "swift") and len(words) > 1:
        n = 2
        if words[1] == "run" and len(words) > 2:
            n = 3
    elif words[0] in ("python", "python3", "py") and len(words) > 2 and words[1] == "-m":
        n = 3
    elif words[0] in ("python", "python3", "py", "node", "bash", "sh", "ruby", "php", "perl", "pwsh", "powershell", "cmd"):
        if len(words) < 2 or words[1].startswith(("-", "/")):
            return ""             # `python -c …`, `bash -c …`: inline code, never offered for always-allow
        n = 2                     # the interpreter plus the script, not every script
    return " ".join(words[:n])


# ── the AI's tools ───────────────────────────────────────────────────────
def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
TOOLS = [
    _fn("list_files", "List the project's files (respects .gitignore). Give a folder to list just that part, and/or a "
        "glob pattern like '*.py' or 'src/**/test_*'.",
        {"path": dict(_S, description="Folder relative to the project root; default the whole project"),
         "pattern": dict(_S, description="Optional glob filter")}),
    _fn("read_file", "Read a text file, with line numbers. For long files read a range of lines.",
        {"path": dict(_S, description="File path relative to the project root"),
         "start_line": dict(_I, description="First line (1-based), optional"),
         "end_line": dict(_I, description="Last line, optional")}, ["path"]),
    _fn("search_code", "Search the project's files for text (or a regular expression). Returns file:line matches.",
        {"query": _S, "regex": dict(_B, description="Treat query as a regular expression"),
         "path": dict(_S, description="Only search under this folder"),
         "pattern": dict(_S, description="Only search files matching this glob, e.g. '*.ts'"),
         "case_sensitive": _B}, ["query"]),
    _fn("edit_file", "Change part of a file by replacing exact text. old_text must match the file exactly "
        "(including indentation) and only once, so include a few surrounding lines. The user reviews the diff.",
        {"path": _S, "old_text": dict(_S, description="Exact existing text to replace"),
         "new_text": dict(_S, description="Replacement text"),
         "replace_all": dict(_B, description="Replace every occurrence instead of exactly one")},
        ["path", "old_text", "new_text"]),
    _fn("write_file", "Create a new file, or replace a whole file's contents. Prefer edit_file for small changes "
        "to existing files. The user reviews the diff.",
        {"path": _S, "content": dict(_S, description="The complete file contents")}, ["path", "content"]),
    _fn("delete_file", "Delete a file. The user reviews it first.", {"path": _S}, ["path"]),
    _fn("run_command", "Run a shell command in the project folder, e.g. tests, a linter, a build or a script, and "
        "get its output. The user approves each command. Don't use it for git push/pull (use the git tools).",
        {"command": _S, "timeout_seconds": dict(_I, description="Default 300, max 1800")}, ["command"]),
    _fn("suggest_tests", "Add tests worth writing or running to the project's Tests list, where the user tracks them. "
        "Use it when you notice untested behaviour, after fixing a bug (a test that would have caught it), or when "
        "asked what to test. Doesn't change any files.",
        {"tests": {"type": "array", "items": {"type": "object", "properties": {
            "title": dict(_S, description="What to test, e.g. 'add() handles negative numbers'"),
            "why": dict(_S, description="Why it matters, one sentence"),
            "file": dict(_S, description="Where the test belongs, optional"),
            "command": dict(_S, description="Command that runs it, if it already exists, optional")},
            "required": ["title"]}}}, ["tests"]),
    _fn("http_request", "Send an HTTP request to an API, e.g. the project's dev server, and get the status, headers and "
        "body back. Use {{variables}} from the active API environment, e.g. {{baseUrl}}/api/users. Reading from "
        "localhost (GET) runs straight away; anything else asks the user first.",
        {"method": {"type": "string", "enum": list(apimod.METHODS)}, "url": _S,
         "headers": {"type": "object", "description": "Header name to value"},
         "json": {"description": "A JSON body (object or array)"}, "body": dict(_S, description="A raw text body")},
        ["method", "url"]),
    _fn("run_api_collection", "Run every request in a saved API collection (from the API tab) with its checks, "
        "and get pass/fail per request. Results also go on the Tests list.", {"name": dict(_S, description="Collection name")},
        ["name"]),
    _fn("list_endpoints", "The HTTP endpoints defined in the project's code (Express, FastAPI, Flask, Django, Spring, "
        "JAX-RS, ASP.NET, Go, Rails, Laravel, OpenAPI files), with file and line."),
    _fn("start_server", "Start the project's dev server in the background (or restart it) and wait until it answers. "
        "The user approves the command. Uses the project's configured server command unless you give one.",
        {"command": dict(_S, description="Optional; defaults to the configured one"), "url": dict(_S, description="Where it listens")}),
    _fn("stop_server", "Stop the project's dev server."),
    _fn("server_logs", "The latest output from the project's dev server (errors, request logs).",
        {"lines": dict(_I, description="How many lines, default 80")}),
    _fn("git_status", "Current branch, how far ahead/behind the remote it is, and the changed files."),
    _fn("git_diff", "Show uncommitted changes as a diff, for one file or the whole project.",
        {"path": dict(_S, description="Optional file")}),
    _fn("git_log", "Recent commits.", {"count": dict(_I, description="How many, default 15")}),
    _fn("git_commit", "Commit changes with a message. Commits all changed files unless you list some. "
        "The user approves it.",
        {"message": _S, "paths": {"type": "array", "items": _S, "description": "Optional files to commit"}},
        ["message"]),
    _fn("git_pull", "Get new commits from the remote. Only fast-forwards unless mode is 'merge' or 'rebase'. "
        "The user approves it.", {"mode": {"type": "string", "enum": ["ff", "merge", "rebase"]}}),
    _fn("git_push", "Push the current branch's commits to the remote (never forced). Only when the user asked. "
        "The user approves every push."),
    _fn("git_branch", "Switch to another branch, or create a new one and switch to it. The user approves it.",
        {"name": _S, "create": _B}, ["name"]),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOLS]
READ_ONLY = {"list_files", "read_file", "search_code", "git_status", "git_diff", "git_log",
             "list_endpoints", "server_logs", "stop_server",
             "suggest_tests"}     # suggest_tests only adds to the app's Tests list
EDITS = {"edit_file", "write_file", "delete_file"}
ALWAYS_ASK = {"git_push"}           # "allow all in this chat" never covers a push

SYSTEM_PROMPT = """You are working in a code project on the user's computer, like a coding assistant inside an editor.
- Look before you change anything: list_files, search_code and read_file. Don't guess file contents.
- Change files with edit_file (exact text replacement; copy old_text from read_file output without the line numbers) \
or write_file (new files, or full rewrites). Keep changes focused on what was asked and match the existing style.
- The user sees each change as a diff and approves or rejects it. If one is rejected, ask what they'd prefer.
- After changing code, run the tests or a quick check with run_command when the project has them, and fix what fails.
- When you spot behaviour that has no test, or fix a bug a test would have caught, add it with suggest_tests.
- Git: use git_status/git_diff/git_log to see the state; git_commit with a clear message when the user wants a commit; \
git_pull and git_push only when the user asks. Never rewrite history.
- APIs: list_endpoints finds the routes in the code; start_server runs the dev server; http_request calls an endpoint so you can see what it really returns (check your fix this way); server_logs shows server errors. Never guess a response.
- If the user seems unsure how to build or run the project, explain the commands in plain words and offer to run them.
- Paths are relative to the project root and use forward slashes.
- When you're done, say briefly what you changed and why."""


class Project:
    def __init__(self, data, manager=None):
        self.data = data
        self.manager = manager

    def test_command(self):
        return (self.data.get("test_command") or "").strip() or testlog.guess_command(self.root)

    def is_test(self, command):
        tc = " ".join(self.test_command().split())
        return testlog.is_test_command(command) or (bool(tc) and " ".join(command.split()) == tc)

    id = property(lambda self: self.data["id"])
    name = property(lambda self: self.data["name"])
    root = property(lambda self: self.data["path"])

    # paths
    def resolve(self, rel, allow_root=True):
        rel = (rel or ".").strip().replace("\\", "/")
        root = os.path.realpath(self.root)
        full = os.path.realpath(rel if os.path.isabs(rel) else os.path.join(root, rel))
        nroot, nfull = os.path.normcase(root), os.path.normcase(full)
        if nfull != nroot and not nfull.startswith(nroot.rstrip(os.sep) + os.sep):
            raise ToolError("%s is outside the project folder." % rel)
        if nfull == nroot and not allow_root:
            raise ToolError("Give a file path, not the project folder.")
        if ".git" in os.path.relpath(full, root).split(os.sep):
            raise ToolError("Files inside .git can't be read or changed; use the git tools.")
        return full

    def rel(self, full):
        return os.path.relpath(full, os.path.realpath(self.root)).replace(os.sep, "/")

    def is_git(self):
        return os.path.exists(os.path.join(self.root, ".git")) or gitops.is_repo(self.root)

    def files(self):
        """Every file in the project that isn't ignored, as relative paths."""
        if gitops.git_path() and self.is_git():
            try:
                out = gitops.ls_files(self.root, IGNORE_DIRS)
                return [p for p in out if os.path.exists(os.path.join(self.root, p))][:MAX_FILES]
            except gitops.GitError:
                pass
        out = []
        for d, dirs, names in os.walk(self.root):
            dirs[:] = sorted(x for x in dirs if x not in IGNORE_DIRS and not x.endswith(".egg-info"))
            for n in sorted(names):
                out.append(os.path.relpath(os.path.join(d, n), self.root).replace(os.sep, "/"))
                if len(out) >= MAX_FILES:
                    return out
        return out

    def outline(self, limit=80):
        """A short picture of the project's layout for the system prompt."""
        files = self.files()
        top = {}
        for p in files:
            parts = p.split("/")
            key = parts[0] + ("/" if len(parts) > 1 else "")
            top.setdefault(key, []).append(p)
        lines = []
        for k in sorted(top, key=lambda x: (not x.endswith("/"), x.lower())):
            if k.endswith("/"):
                subs = sorted({"/".join(p.split("/")[:2]) + ("/" if p.count("/") > 1 else "") for p in top[k]})
                lines.append("%s (%d files)" % (k, len(top[k])))
                lines += ["  " + s for s in subs[:12]] + (["  …"] if len(subs) > 12 else [])
            else:
                lines.append(k)
        more = len(lines) - limit
        return "\n".join(lines[:limit]) + ("\n…" if more > 0 else ""), len(files)

    def system_prompt(self):
        tree, n = self.outline()
        head = ["Project: %s" % self.name, "Folder: %s" % self.root, "%d files" % n]
        if self.is_git() and gitops.git_path():
            try:
                st = gitops.status(self.root)
                b = st["branch"] + (" (tracking %s, %d ahead, %d behind)" % (st["upstream"], st["ahead"], st["behind"])
                                    if st["upstream"] else " (not on the remote yet)")
                head.append("Git branch: " + b)
                if st["files"]:
                    head.append("Uncommitted changes in %d file%s" % (len(st["files"]), "" if len(st["files"]) == 1 else "s"))
                if st["state"]:
                    head.append("A %s is in progress with conflicts to resolve." % st["state"])
            except gitops.GitError:
                pass
        else:
            head.append("Not a Git repository")
        kind = taskmod.detect(self.root)
        if kind["type"]:
            head.append("Project type: " + kind["type"])
        if kind["tasks"]:
            head.append("Commands: " + "; ".join("%s = `%s`" % (t["name"], t["command"]) for t in kind["tasks"]))
        if self.manager is not None and getattr(self.manager, "servers", None) is not None:
            srv = self.server_config()
            st = self.manager.servers.status(self.id)["state"]
            head.append("Dev server: %s (%s)%s" % (srv["command"] or "not set", st, ", at " + srv["url"] if srv["url"] else ""))
            vals, _ = self.api_vars()
            if vals:
                head.append("API environment variables: " + ", ".join("{{%s}}" % k for k in vals))
            cols = [c["name"] for c in self.manager.api.scope(self.id)["collections"]]
            if cols:
                head.append("Saved API collections: " + ", ".join(cols))
        tc = self.test_command()
        head.append("Test command: " + tc if tc else "No test command known yet")
        if self.manager is not None:
            brief = self.manager.tests.brief(self.id)
            if brief:
                head.append(brief)
        notes = (self.data.get("notes") or "").strip()
        out = SYSTEM_PROMPT + "\n\n" + "\n".join(head) + "\n\nLayout:\n" + tree
        if notes:
            out += "\n\nThe user's notes about this project (follow them):\n" + notes
        return out

    # previews: what a tool call would do, shown to the user before it runs
    def preview(self, name, a):
        a = a or {}
        if name in ("edit_file", "write_file", "delete_file"):
            rel, before, after, fmt = self._change(name, a)
            standards = self.check_standards(name, rel, before, after)
            d = unified(rel, before, after)
            add, rem = diff_counts(d)
            return {"kind": "diff", "path": rel, "diff": d, "added": add, "removed": rem, "standards": standards,
                    "action": "create" if before is None else "delete" if after is None else "edit"}
        if name == "run_command":
            cmd = str(a.get("command") or "").strip()
            if not cmd:
                raise ToolError("No command given.")
            if re.search(r"\bgit\b[^\n]*\b(push|pull)\b", cmd):
                raise ToolError("Use the git_push / git_pull tools instead of running git push or pull.")
            return {"kind": "command", "command": cmd, "cwd": self.root, "prefix": suggest_prefix(cmd),
                    "allowed": command_allowed(cmd, self.data.get("allowed_commands"))}
        if name == "http_request":
            req = self.http_from_args(a)
            vals, secrets = self.api_vars()
            b = apimod.build(req, vals)
            if not b["url"] or not apimod.urllib.parse.urlsplit(b["url"]).hostname:
                raise ToolError("Give a full URL, e.g. {{baseUrl}}/api/users or http://localhost:3000/api/users.")
            return {"kind": "http", "method": b["method"], "url": apimod.mask(b["url"], secrets),
                    "headers": [[k, apimod.mask(v, secrets)] for k, v in b["headers"] if k not in ("User-Agent", "Accept")],
                    "body": apimod.mask((b["data"] or b"").decode("utf-8", "replace"), secrets)[:4000],
                    "safe": b["method"] in apimod.SAFE_METHODS and apimod.is_local(b["url"]), "missing": b["missing"]}
        if name == "run_api_collection":
            col = self.find_collection(a.get("name"))
            if not col:
                names = [c["name"] for c in self.manager.api.scope(self.id)["collections"]]
                raise ToolError("No API collection called %r. Saved collections: %s" % (a.get("name"), ", ".join(names) or "none"))
            vals, _ = self.api_vars()
            reqs = apimod.requests_in(col)
            return {"kind": "api_run", "name": col["name"], "requests": ["%s %s" % (r["method"], r["name"]) for r in reqs],
                    "safe": all(apimod.is_safe(r, vals) for r in reqs)}
        if name == "start_server":
            cfg = self.server_config()
            cmd = str(a.get("command") or cfg["command"]).strip()
            if not cmd:
                raise ToolError("There's no server command for this project yet. Ask the user how they start it, or give one.")
            return {"kind": "command", "command": cmd, "cwd": self.root, "prefix": suggest_prefix(cmd), "server": True,
                    "allowed": command_allowed(cmd, self.data.get("allowed_commands"))}
        if name == "git_push":
            pv = gitops.push_preview(self.root)
            if pv["branch"] in (self.data.get("protected") or []):
                raise ToolError("The user has protected the %s branch: the AI can't push to it. Create a branch "
                                "with git_branch and push that, or ask the user to push." % pv["branch"])
            if not pv["commits"] and not pv["new_branch"]:
                raise ToolError("There's nothing to push: the remote already has every commit.")
            return dict(pv, kind="push")
        if name == "git_commit":
            st = gitops.status(self.root)
            paths = [p for p in (a.get("paths") or []) if isinstance(p, str)]
            files = [f for f in st["files"] if not paths or f["path"] in paths]
            if not files:
                raise ToolError("There's nothing to commit.")
            return {"kind": "commit", "message": str(a.get("message") or ""),
                    "files": [{"path": f["path"], "status": f["status"]} for f in files]}
        if name == "git_pull":
            return {"kind": "pull", "mode": a.get("mode") or "ff"}
        if name == "git_branch":
            return {"kind": "branch", "name": str(a.get("name") or ""), "create": bool(a.get("create"))}
        return None

    # APIs and the dev server
    def server_config(self):
        cfg = self.data.get("dev_server") or {}
        cmd, url = cfg.get("command", ""), cfg.get("url", "")
        if not cmd:
            gcmd, gurl = apimod.guess_server(self.root)
            cmd, url = gcmd, url or gurl
        return {"command": cmd, "url": url or "http://localhost:3000"}

    def api_vars(self):
        env = self.manager.api.env(self.id) if getattr(self.manager, "api", None) is not None else None
        vals, secrets = apimod.variables(env)
        if "baseUrl" not in vals:
            vals["baseUrl"] = self.server_config()["url"].rstrip("/")
        return vals, secrets

    def http_from_args(self, a):
        hdrs = a.get("headers") if isinstance(a.get("headers"), dict) else {}
        req = apimod.blank_request("AI request", str(a.get("method") or "GET").upper(), str(a.get("url") or ""))
        req["headers"] = [{"key": str(k), "value": str(v), "enabled": True} for k, v in hdrs.items()]
        if a.get("json") is not None:
            req["body"] = {"mode": "json", "text": a["json"] if isinstance(a["json"], str) else json.dumps(a["json"]), "form": []}
        elif a.get("body"):
            req["body"] = {"mode": "text", "text": str(a["body"]), "form": []}
        return req

    def find_collection(self, name):
        cols = self.manager.api.scope(self.id)["collections"]
        n = (name or "").strip().lower()
        return next((c for c in cols if c["name"].lower() == n), None) or next((c for c in cols if n and n in c["name"].lower()), None)

    def check_standards(self, name, rel, before, after):
        """Documentation standards that apply to this file. A whole-file write that misses a required section,
        or an edit that removes one, is refused, so the AI has to follow the standard."""
        lib = getattr(self.manager, "library", None)
        if lib is None or after is None:
            return []
        out = []
        for std in lib.standards_for_path(rel):
            text = lib.read("standards", std)
            r = libmod.check_document(after, text)
            if not r["required"]:
                continue
            problem = ""
            if name == "write_file" or before is None:
                if r["missing"]:
                    problem = "it's missing these sections: " + ", ".join(r["missing"])
                elif not r["in_order"]:
                    problem = "its sections aren't in the standard's order"
            else:
                was = libmod.check_document(before, text)
                lost = [m for m in r["missing"] if m not in was["missing"]]
                if lost:
                    problem = "this edit removes these required sections: " + ", ".join(lost)
                elif was["in_order"] and not r["in_order"]:
                    problem = "this edit puts the sections out of the standard's order"
            if problem:
                raise ToolError('%s has to follow the documentation standard "%s", but %s. Write it with these '
                                'headings, in this order: %s. The standard is in your instructions.'
                                % (rel, std, problem, " / ".join(r["required"])))
            out.append({"name": std, "missing": r["missing"]})
        return out

    def _change(self, name, a):
        """(relative path, text before or None, text after or None, format) for a file change."""
        full = self.resolve(a.get("path"), allow_root=False)
        rel = self.rel(full)
        exists = os.path.isfile(full)
        if os.path.isdir(full):
            raise ToolError("%s is a folder." % rel)
        before, fmt = read_text(full) if exists else (None, {})
        if name == "write_file":
            content = a.get("content")
            if not isinstance(content, str):
                raise ToolError("write_file needs the full file content.")
            after = content.replace("\r\n", "\n")
            if before is not None and after == before:
                raise ToolError("That's already the file's content; nothing changes.")
            return rel, before, after, fmt
        if name == "delete_file":
            if not exists:
                raise ToolError("%s doesn't exist." % rel)
            return rel, before, None, fmt
        if not exists:
            raise ToolError("%s doesn't exist. Use write_file to create it." % rel)
        old = str(a.get("old_text") or "").replace("\r\n", "\n")
        new = str(a.get("new_text") if a.get("new_text") is not None else "").replace("\r\n", "\n")
        if not old:
            raise ToolError("old_text is empty. Give the exact text to replace.")
        if old == new:
            raise ToolError("old_text and new_text are the same; nothing changes.")
        n = before.count(old)
        if n == 0:
            stripped = re.sub(r"^\s*\d+\u2502 ?", "", old, flags=re.M)   # copied with read_file's line numbers
            if stripped != old and before.count(stripped) >= 1:
                old, n = stripped, before.count(stripped)
                new = re.sub(r"^\s*\d+\u2502 ?", "", new, flags=re.M)
        if n == 0:
            hint = _near(before, old)
            raise ToolError("old_text wasn't found in %s. It has to match exactly, including spaces and indentation. "
                            "Read the file again and copy the text.%s" % (rel, hint))
        if n > 1 and not a.get("replace_all"):
            raise ToolError("old_text appears %d times in %s. Include more surrounding lines so it matches once, "
                            "or set replace_all." % (n, rel))
        after = before.replace(old, new) if a.get("replace_all") else before.replace(old, new, 1)
        return rel, before, after, fmt

    def apply(self, name, a):
        """Make a file change. Returns (relative path, before, after) for undo."""
        rel, before, after, fmt = self._change(name, a)
        full = self.resolve(rel, allow_root=False)
        if after is None:
            os.remove(full)
        else:
            write_text(full, after, fmt)
        return rel, before, after

    # running a tool
    def run(self, name, a, cancel=None):
        a = a or {}
        if name == "list_files":
            files = self.files()
            sub = (a.get("path") or "").strip().strip("/").replace("\\", "/")
            if sub and sub != ".":
                self.resolve(sub)
                files = [p for p in files if p == sub or p.startswith(sub + "/")]
            pat = (a.get("pattern") or "").strip()
            if pat:
                files = [p for p in files if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], pat)]
            if not files:
                return "No files match."
            shown = files[:500]
            return "%d file%s%s:\n%s%s" % (len(files), "" if len(files) == 1 else "s", " (first 500)" if len(files) > 500 else "",
                                           "\n".join(shown), "\n…" if len(files) > 500 else "")
        if name == "read_file":
            full = self.resolve(a.get("path"), allow_root=False)
            if os.path.isdir(full):
                raise ToolError("%s is a folder; use list_files." % self.rel(full))
            text, _ = read_text(full)
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            total = len(lines)
            s = max(1, int(a.get("start_line") or 1))
            e = int(a.get("end_line") or 0) or total
            e = min(e, total, s + MAX_READ_LINES - 1)
            width = len(str(e))
            body = "\n".join("%*d\u2502 %s" % (width, i, lines[i - 1]) for i in range(s, e + 1))
            note = ""
            if s > 1 or e < total:
                note = "\n(lines %d-%d of %d%s)" % (s, e, total, "; read more with start_line" if e < total else "")
            return "%s (%d lines)\n%s%s" % (self.rel(full), total, body or "(empty file)", note)
        if name == "search_code":
            return self.search(a)
        if name == "list_endpoints":
            routes = apimod.find_routes(self)
            if not routes:
                return "No HTTP endpoints found in the code."
            return "%d endpoint%s:\n%s" % (len(routes), "" if len(routes) == 1 else "s", "\n".join(
                "%-7s %s   (%s%s)" % (r["method"], r["path"], r["file"], ":%d" % r["line"] if r["line"] else "") for r in routes[:300]))
        if name in ("http_request", "run_api_collection", "start_server", "stop_server", "server_logs"):
            return self.run_api(name, a, cancel)
        if name == "suggest_tests":
            if self.manager is None:
                raise ToolError("The Tests list isn't available.")
            added = self.manager.tests.suggest(self.id, a.get("tests"), "ai")
            return ("Added %d test%s to the Tests list: %s" % (len(added), "" if len(added) == 1 else "s",
                    "; ".join(s["title"] for s in added))) if added else "Those tests are already on the list."
        if name in EDITS:
            rel, before, after = self.apply(name, a)
            if after is None:
                return "Deleted %s." % rel
            add, rem = diff_counts(unified(rel, before, after))
            return "%s %s (+%d -%d)." % ("Created" if before is None else "Updated", rel, add, rem)
        if name == "run_command":
            cmd = str(a.get("command") or "").strip()
            t = max(5, min(int(a.get("timeout_seconds") or CMD_TIMEOUT), 1800))
            t0 = time.time()
            code, out = run_command(cmd, self.root, t, cancel)
            if self.manager is not None and self.is_test(cmd):
                run = self.manager.tests.add_run(self.id, cmd, code, out, "ai", time.time() - t0)
                out = out.rstrip() + "\n[test summary: %s]" % run["summary"]
            return "$ %s\n%s\n[exit code %s]" % (cmd, clip(out.strip() or "(no output)"), "none" if code is None else code)
        if not gitops.git_path():
            raise ToolError("Git isn't installed on this computer.")
        if not self.is_git():
            raise ToolError("This project isn't a Git repository.")
        if name == "git_status":
            st = gitops.status(self.root)
            lines = ["Branch: %s" % st["branch"]]
            lines.append("Remote branch: %s (%d ahead, %d behind)" % (st["upstream"], st["ahead"], st["behind"])
                         if st["upstream"] else "Not pushed to a remote yet")
            if st["state"]:
                lines.append("A %s is in progress." % st["state"])
            lines.append("Changed files:" if st["files"] else "No uncommitted changes.")
            lines += ["  %-9s %s%s" % (f["status"], f["path"], " (from %s)" % f["orig"] if f["orig"] else "")
                      for f in st["files"][:300]]
            return "\n".join(lines)
        if name == "git_diff":
            path = (a.get("path") or "").strip()
            if path:
                full = self.resolve(path, allow_root=False)
                path = self.rel(full)
                st = gitops.status(self.root)
                if any(f["path"] == path and f["code"] == "?" for f in st["files"]):
                    return clip(gitops.diff(self.root, path, untracked_text=read_text(full)[0]))
            d = gitops.diff(self.root, path or None)
            if not path:
                new = [f["path"] for f in gitops.status(self.root)["files"] if f["code"] == "?"]
                if new:
                    d += "\nNew files not shown above: " + ", ".join(new[:50])
            return clip(d.strip() or "No uncommitted changes.")
        if name == "git_log":
            rows = gitops.log(self.root, max(1, min(int(a.get("count") or 15), 100)))
            return "\n".join("%s  %s  (%s, %s)" % (r["hash"], r["subject"], r["author"], r["when"]) for r in rows) \
                or "No commits yet."
        if name == "git_commit":
            paths = [p for p in (a.get("paths") or []) if isinstance(p, str)] or None
            c = gitops.commit(self.root, a.get("message"), paths)
            return "Committed %s: %s" % (c["hash"], c["subject"])
        if name == "git_pull":
            out = gitops.pull(self.root, a.get("mode") if a.get("mode") in ("ff", "merge", "rebase") else "ff")
            return "Pulled.\n" + clip(out.strip())
        if name == "git_push":
            pv = gitops.push(self.root)
            return "Pushed %s to %s (%d commit%s)." % (pv["branch"], pv["remote"], len(pv["commits"]),
                                                       "" if len(pv["commits"]) == 1 else "s")
        if name == "git_branch":
            gitops.switch(self.root, a.get("name"), bool(a.get("create")))
            return "Now on branch %s." % a.get("name")
        raise ToolError("Unknown tool %s." % name)

    def run_api(self, name, a, cancel=None):
        m = self.manager
        if m is None or getattr(m, "servers", None) is None:
            raise ToolError("The API tools aren't available.")
        if name == "http_request":
            req = self.http_from_args(a)
            vals, secrets = self.api_vars()
            res = apimod.send(req, vals)
            m.api.add_history(self.id, req, res, "ai")
            if res.get("error"):
                hint = ""
                if "refused" in res["error"].lower():
                    st = m.servers.status(self.id)["state"]
                    hint = " The dev server is %s; start it with start_server." % st
                return apimod.mask("%s %s\nError: %s%s" % (res["method"], res["url"], res["error"], hint), secrets)
            hdr = "\n".join("%s: %s" % (k, v) for k, v in res["headers"]
                             if k.lower() in ("content-type", "location", "set-cookie", "www-authenticate", "allow", "cache-control"))
            text = "%s %s\nHTTP %s %s · %s ms · %s bytes%s\n%s\n\n%s" % (
                res["method"], res["url"], res["status"], res["reason"], res["ms"], res["size"],
                " (missing variables: %s)" % ", ".join(res["missing"]) if res["missing"] else "", hdr, apimod.pretty_body(res))
            return apimod.mask(text, secrets)
        if name == "run_api_collection":
            from . import apirun
            col = self.find_collection(a.get("name"))
            env = m.api.env(self.id)
            rep = apirun.Runner(m.api, self.id, getattr(m, "script_runner", None),
                                defaults={"baseUrl": self.server_config()["url"].rstrip("/")}).run(col, env)
            text = apirun.text_report(rep, apimod.variables(env)[1])
            m.tests.add_run(self.id, "API: " + col["name"], 1 if rep["failed"] else 0, text, "ai", rep["seconds"])
            return text
        if name == "start_server":
            cfg = self.server_config()
            cmd = str(a.get("command") or cfg["command"]).strip()
            url = str(a.get("url") or cfg["url"]).strip()
            m.servers.stop(self.id)
            m.servers.start(self.id, cmd, url, self.root)
            st = m.servers.wait_ready(self.id, 90)
            tail = "\n".join(st.get("log", "").splitlines()[-30:])
            if st["state"] == "exited":
                return "The server stopped straight away (exit code %s). Its output:\n%s" % (st.get("code"), tail)
            listening = apimod.Servers.listening(url)
            return "Server %s: `%s`, %s at %s.\nRecent output:\n%s" % (
                "running" if listening else "started", cmd, "answering" if listening else "not answering yet", url, tail)
        if name == "stop_server":
            m.servers.stop(self.id)
            return "Stopped the dev server."
        if name == "server_logs":
            st = m.servers.status(self.id)
            n = max(5, min(int(a.get("lines") or 80), 400))
            lines = st.get("log", "").splitlines()[-n:]
            return "Server is %s.\n%s" % (st["state"], "\n".join(lines) or "(no output yet)")

    def search(self, a):
        q = str(a.get("query") or "")
        if not q:
            raise ToolError("Give something to search for.")
        flags = 0 if a.get("case_sensitive") else re.I
        try:
            rx = re.compile(q if a.get("regex") else re.escape(q), flags)
        except re.error as e:
            raise ToolError("That regular expression doesn't work: %s" % e)
        files = self.files()
        sub = (a.get("path") or "").strip().strip("/").replace("\\", "/")
        if sub and sub != ".":
            files = [p for p in files if p == sub or p.startswith(sub + "/")]
        pat = (a.get("pattern") or "").strip()
        if pat:
            files = [p for p in files if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], pat)]
        hits, nfiles = [], 0
        for p in files:
            full = os.path.join(self.root, p)
            try:
                if os.path.getsize(full) > 1024 * 1024:
                    continue
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            text = data.decode("utf-8", "replace")
            found = False
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    found = True
                    hits.append("%s:%d: %s" % (p, i, line.strip()[:200]))
                    if len(hits) >= 150:
                        break
            nfiles += found
            if len(hits) >= 150:
                break
        if not hits:
            return "No matches for %r." % q
        return "%d match%s in %d file%s%s:\n%s" % (len(hits), "" if len(hits) == 1 else "es", nfiles,
                                                   "" if nfiles == 1 else "s", " (first 150)" if len(hits) >= 150 else "",
                                                   "\n".join(hits))


def _near(text, old):
    """A hint showing where the closest match to old_text's first line is."""
    first = next((l.strip() for l in old.split("\n") if l.strip()), "")
    if not first:
        return ""
    lines = text.split("\n")
    best = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.6)
    if not best:
        return ""
    i = [l.strip() for l in lines].index(best[0])
    return " The closest line is %d: %s" % (i + 1, lines[i].strip()[:160])


# ── the list of projects ─────────────────────────────────────────────────
def default_parent():
    home = os.path.expanduser("~")
    for name in ("Projects", "projects", "source/repos", "code", "dev", "src"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return p
    return os.path.join(home, "Projects")


class Projects:
    def __init__(self, store):
        self.store = store
        self.changes = {}     # change id -> {"project", "path", "before", "after"} for undo
        self.terms = {}       # project id -> {"proc", "id"} for the Terminal panel
        self.lock = threading.Lock()
        self.tests = testlog.TestLog(store.dir)

    def all(self):
        return self.store.settings.setdefault("projects", [])

    def get(self, pid):
        d = next((p for p in self.all() if p["id"] == pid), None)
        return Project(d, self) if d else None

    def for_chat(self, c):
        p = self.get(c.get("project") or "") if c.get("project") else None
        return p if p and os.path.isdir(p.root) else None

    def summary(self, p):
        exists = os.path.isdir(p["path"])
        return {"id": p["id"], "name": p["name"], "path": p["path"], "exists": exists,
                "notes": p.get("notes", ""), "allowed_commands": p.get("allowed_commands", []),
                "protected": p.get("protected", []), "test_command": p.get("test_command", ""),
                "test_guess": testlog.guess_command(p["path"]) if exists else ""}

    def add_folder(self, path):
        path = os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))
        if not os.path.isdir(path):
            raise ValueError("That folder doesn't exist: %s" % path)
        if gitops.git_path() and gitops.is_repo(path):
            try:
                top = gitops.toplevel(path)
                if top and os.path.isdir(top):
                    path = top
            except gitops.GitError:
                pass
        for p in self.all():
            if os.path.normcase(os.path.abspath(p["path"])) == os.path.normcase(path):
                return p
        d = {"id": new_id(), "name": os.path.basename(path.rstrip("\\/")) or path, "path": path, "notes": "",
             "allowed_commands": [], "protected": []}
        self.all().insert(0, d)
        self.store.save_settings()
        return d

    def clone(self, url, parent=None, name=None):
        parent = os.path.abspath(os.path.expanduser((parent or "").strip() or default_parent()))
        base = name or re.sub(r"\.git$", "", (url or "").rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "project"
        base = re.sub(r"[^\w.\-]+", "-", base).strip(".-") or "project"
        dest = _unique(os.path.join(parent, base))
        gitops.clone(url, dest)
        return self.add_folder(dest)

    def import_zip(self, filename, data, parent=None):
        parent = os.path.abspath(os.path.expanduser((parent or "").strip() or default_parent()))
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("That isn't a ZIP file.")
        names = [n for n in zf.namelist() if n and not n.startswith("__MACOSX/")]
        if not names:
            raise ValueError("The ZIP file is empty.")
        if sum(i.file_size for i in zf.infolist()) > 1024 * 1024 * 1024:
            raise ValueError("That ZIP unpacks to more than 1 GB.")
        tops = {n.replace("\\", "/").split("/", 1)[0] for n in names}
        strip = len(tops) == 1 and all("/" in n.replace("\\", "/") or n.endswith("/") for n in names)
        base = (list(tops)[0] if strip else os.path.splitext(os.path.basename(filename or "project.zip"))[0]) or "project"
        base = re.sub(r"[^\w.\-]+", "-", base).strip(".-") or "project"
        dest = _unique(os.path.join(parent, base))
        os.makedirs(dest)
        root = os.path.realpath(dest)
        for info in zf.infolist():
            n = info.filename.replace("\\", "/")
            if n.startswith("__MACOSX/"):
                continue
            if strip:
                n = n.split("/", 1)[1] if "/" in n else ""
            if not n or n.endswith("/"):
                continue
            target = os.path.realpath(os.path.join(root, *n.split("/")))
            if not target.startswith(root + os.sep):
                continue              # "../" tricks in the archive are skipped
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
        return self.add_folder(dest)

    def update(self, pid, b):
        d = next((p for p in self.all() if p["id"] == pid), None)
        if not d:
            raise ValueError("Unknown project.")
        if "name" in b and str(b["name"]).strip():
            d["name"] = str(b["name"]).strip()[:80]
        if "notes" in b:
            d["notes"] = str(b["notes"])[:20000]
        if isinstance(b.get("allowed_commands"), list):
            d["allowed_commands"] = [" ".join(str(x).split())[:200] for x in b["allowed_commands"] if str(x).strip()][:100]
        if "test_command" in b:
            d["test_command"] = " ".join(str(b["test_command"]).split())[:300]
        if isinstance(b.get("dev_server"), dict):
            d["dev_server"] = {"command": " ".join(str(b["dev_server"].get("command") or "").split())[:300],
                               "url": str(b["dev_server"].get("url") or "").strip()[:300]}
        if isinstance(b.get("protected"), list):
            d["protected"] = [str(x).strip()[:100] for x in b["protected"] if str(x).strip()][:50]
        self.store.save_settings()
        return d

    def allow_command(self, pid, prefix):
        d = next((p for p in self.all() if p["id"] == pid), None)
        prefix = " ".join((prefix or "").split())
        if d and prefix and prefix not in d.setdefault("allowed_commands", []):
            d["allowed_commands"].append(prefix)
            self.store.save_settings()

    def remove(self, pid):
        self.store.settings["projects"] = [p for p in self.all() if p["id"] != pid]
        self.store.save_settings()
        self.tests.forget(pid)

    # undo of AI file changes
    def record(self, pid, rel, before, after):
        cid = new_id()
        with self.lock:
            self.changes[cid] = {"project": pid, "path": rel, "before": before, "after": after}
            if len(self.changes) > 300:
                for k in list(self.changes)[:50]:
                    del self.changes[k]
        return cid

    def undo(self, cid):
        ch = self.changes.get(cid)
        if not ch:
            raise ValueError("That change can't be undone any more (the app was restarted or it's too old).")
        p = self.get(ch["project"])
        if not p:
            raise ValueError("That project was removed.")
        full = p.resolve(ch["path"], allow_root=False)
        try:
            now, fmt = read_text(full) if os.path.isfile(full) else (None, {})
        except ToolError:
            now, fmt = "\0", {}
        if now != ch["after"]:
            raise ValueError("%s has changed since, so it can't be undone safely." % ch["path"])
        if ch["before"] is None:
            os.remove(full)
        else:
            write_text(full, ch["before"], fmt)
        del self.changes[cid]
        return ch["path"]

    # the Terminal panel (commands the user types; they run without asking)
    def term_start(self, pid, command, publish, test=False, suggestion=None):
        p = self.get(pid)
        if not p or not os.path.isdir(p.root):
            raise ValueError("Unknown project.")
        command = (command or "").strip()
        if not command:
            raise ValueError("Type a command.")
        with self.lock:
            cur = self.terms.get(pid)
            if cur and cur["proc"].poll() is None:
                raise ValueError("A command is still running. Stop it first.")
            proc = spawn(command, p.root)
            rid = new_id()
            self.terms[pid] = {"proc": proc, "id": rid}
        record = test or p.is_test(command)     # test runs go on the Tests list
        t0 = time.time()

        def pump():
            buf, last, whole = [], [time.time()], []

            def flush():
                if buf:
                    publish({"type": "term", "project": pid, "run": rid, "data": "".join(buf)})
                    buf.clear()
                last[0] = time.time()

            def on_text(t):
                t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", t)
                buf.append(t)
                if record:
                    whole.append(t)
                if time.time() - last[0] > 0.08 or sum(map(len, buf)) > 8000:
                    flush()
            try:
                stream(proc, on_text)
            finally:
                flush()
                code = proc.wait()
                killed = self.terms.get(pid, {}).get("stopped") == rid
                if record:
                    self.tests.add_run(pid, command, None if killed else code, "".join(whole).replace("\r\n", "\n"),
                                       "you", time.time() - t0, suggestion=suggestion)
                publish({"type": "term_done", "project": pid, "run": rid, "code": code})
        threading.Thread(target=pump, daemon=True).start()
        return rid

    def term_stop(self, pid):
        cur = self.terms.get(pid)
        if cur:
            cur["stopped"] = cur["id"]
            kill_tree(cur["proc"])


def _unique(path):
    if not os.path.exists(path):
        return path
    n = 2
    while os.path.exists("%s-%d" % (path, n)):
        n += 1
    return "%s-%d" % (path, n)


def list_dirs(path):
    """Folders inside `path`, for the folder picker. Empty path: the home folder (and drives on Windows)."""
    path = (path or "").strip()
    if not path:
        path = os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise ValueError("That folder doesn't exist.")
    try:
        names = sorted((n for n in os.listdir(path) if not n.startswith(".") and os.path.isdir(os.path.join(path, n))),
                       key=str.lower)
    except PermissionError:
        names = []
    parent = os.path.dirname(path.rstrip("\\/")) or None
    if parent == path:
        parent = None
    drives = []
    if sys.platform == "win32":
        drives = ["%s:\\" % d for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir("%s:\\" % d)]
    return {"path": path, "parent": parent, "dirs": names[:2000], "home": os.path.expanduser("~"),
            "is_repo": os.path.exists(os.path.join(path, ".git")), "drives": drives}
