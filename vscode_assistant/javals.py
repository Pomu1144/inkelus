"""Java support for Projects: the Eclipse JDT language server (the same one VS Code's Java support uses).

It gives the editor what NetBeans has: errors as you type, completion, go to definition (into library
classes too), find usages, rename, quick fixes, organize imports, formatting and an outline. The page
talks to it through Hub.java (see server.py); this module only speaks LSP to the server process.

Nothing is bundled: "Set up Java support" downloads the language server (about 50 MB, from
download.eclipse.org) and the java-debug plugin (from Maven Central) into the app's data folder, once.
It needs a JDK on the computer (21+, or 17+ for an older language server); projects can still target
older Java versions.
"""
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request

from . import net, testlog

# The language server needs Java 21 to run since 1.37; 1.36 is the last that runs on Java 17.
JDTLS = [
    (21, "1.50.0", "jdt-language-server-1.50.0-202509041425.tar.gz"),
    (17, "1.36.0", "jdt-language-server-1.36.0-202405301306.tar.gz"),
]
JDTLS_URL = "https://download.eclipse.org/jdtls/milestones/%s/%s"
DEBUG_VERSION = "0.53.1"
DEBUG_URL = ("https://repo1.maven.org/maven2/com/microsoft/java/com.microsoft.java.debug.plugin/%s/"
             "com.microsoft.java.debug.plugin-%s.jar" % (DEBUG_VERSION, DEBUG_VERSION))

# what the page may ask the language server
PAGE_METHODS = {
    "textDocument/completion", "completionItem/resolve", "textDocument/hover", "textDocument/signatureHelp",
    "textDocument/definition", "textDocument/typeDefinition", "textDocument/implementation",
    "textDocument/references", "textDocument/documentSymbol", "textDocument/documentHighlight",
    "textDocument/formatting", "textDocument/rangeFormatting", "textDocument/onTypeFormatting",
    "textDocument/prepareRename", "textDocument/rename", "textDocument/codeAction", "codeAction/resolve",
    "textDocument/foldingRange", "textDocument/selectionRange", "java/classFileContents", "workspace/symbol",
    "workspace/executeCommand",
}
PAGE_COMMANDS = {
    "vscode.java.resolveMainClass", "vscode.java.resolveClasspath", "vscode.java.validateLaunchConfig",
    "vscode.java.fetchUsageData", "java.project.getAll", "java.project.getSettings", "java.edit.stringFormatting",
}
SKIP_DIRS = {".git", "target", "build", "out", "bin", "dist", "node_modules", ".gradle", ".idea", ".settings", "nbproject"}
WATCH = re.compile(r".*\.(java|properties|xml|gradle|kts)$")


def java_version(java):
    """Major version of a java executable, or 0."""
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return 0
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major      # "1.8.0" is Java 8


def find_javas(extra=""):
    """Java executables on this computer, best first: [(path, major)]."""
    exe = "java.exe" if sys.platform == "win32" else "java"
    cands = []
    if extra:
        cands.append(os.path.join(extra, "bin", exe) if os.path.isdir(extra) else extra)
    if os.environ.get("JAVA_HOME"):
        cands.append(os.path.join(os.environ["JAVA_HOME"], "bin", exe))
    if shutil.which("java"):
        cands.append(shutil.which("java"))
    pats = []
    if sys.platform == "win32":
        for base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"))):
            pats += [os.path.join(base, v, "*", "bin", exe) for v in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "Amazon Corretto", "BellSoft")]
    elif sys.platform == "darwin":
        pats.append("/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java")
    else:
        pats += ["/usr/lib/jvm/*/bin/java", "/opt/java/*/bin/java", "/opt/jdk*/bin/java"]
    pats.append(os.path.join(os.path.expanduser("~"), ".sdkman", "candidates", "java", "*", "bin", exe))
    for pat in pats:
        cands += sorted(glob.glob(pat), reverse=True)
    seen, out = set(), []
    for c in cands:
        real = os.path.realpath(c)
        if real in seen or not os.path.isfile(real):
            continue
        seen.add(real)
        v = java_version(c)
        if v:
            out.append((c, v))
    chosen = os.path.realpath(cands[0]) if extra and cands else None
    out.sort(key=lambda x: (os.path.realpath(x[0]) != chosen, -x[1]))      # the one set in Settings first
    return out


def file_uri(path):
    path = os.path.abspath(path).replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path                                   # C:/x on Windows
    return "file://" + urllib.parse.quote(path, safe="/:")


def uri_path(uri):
    if not uri.startswith("file://"):
        return None
    p = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]
    return os.path.normpath(p)


# ── LSP and DAP messages: a Content-Length header, then JSON ──────────────────
def read_message(stream):
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is not None:
                break
            continue
        k, _, v = line.decode("ascii", "replace").partition(":")
        if k.lower() == "content-length":
            length = int(v.strip())
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def write_message(stream, lock, msg):
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    with lock:
        stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        stream.flush()


class LspError(Exception):
    pass


class Lsp:
    """A JSON-RPC connection to a language server on stdin/stdout."""

    def __init__(self, proc, on_notify, on_request):
        self.proc, self.on_notify, self.on_request = proc, on_notify, on_request
        self.lock, self.next_id, self.pending, self.closed = threading.Lock(), 1, {}, False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                msg = read_message(self.proc.stdout)
                if msg is None:
                    break
                if "method" in msg and "id" in msg:             # the server asks us something
                    try:
                        result = self.on_request(msg["method"], msg.get("params"))
                        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                    except Exception as e:  # noqa: BLE001 - answer with the error rather than hang the server
                        self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})
                elif "method" in msg:
                    try:
                        self.on_notify(msg["method"], msg.get("params"))
                    except Exception:  # noqa: BLE001
                        pass
                elif msg.get("id") in self.pending:
                    box = self.pending.pop(msg["id"])
                    box["msg"] = msg
                    box["event"].set()
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True
            for box in list(self.pending.values()):
                box["event"].set()

    def _send(self, msg):
        if self.closed:
            raise LspError("The Java language server stopped.")
        try:
            write_message(self.proc.stdin, self.lock, msg)
        except OSError:
            self.closed = True
            raise LspError("The Java language server stopped.")

    def request(self, method, params, timeout=30):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        box = {"event": threading.Event(), "msg": None}
        self.pending[rid] = box
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not box["event"].wait(timeout):
            self.pending.pop(rid, None)
            try:
                self.notify("$/cancelRequest", {"id": rid})
            except LspError:
                pass
            raise LspError("The Java language server took too long to answer (%s)." % method)
        msg = box["msg"]
        if msg is None:
            raise LspError("The Java language server stopped.")
        if "error" in msg:
            raise LspError(msg["error"].get("message") or "Java language server error")
        return msg.get("result")

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})


# ── one-time setup: Java and the downloads ────────────────────────────────────
class Tools:
    def __init__(self, data_dir, settings, publish):
        self.base = os.path.join(data_dir, "tools")
        self.settings = settings            # the "java" section of the app's settings
        self.publish = publish
        self.busy = False
        self.error = ""
        self._javas = None

    def javas(self, refresh=False):
        if self._javas is None or refresh:
            self._javas = find_javas(self.settings.get("java_home") or "")
        return self._javas

    def runtime(self):
        """(java, major, jdtls version, tarball) to run the language server with, or (None, …) when no Java fits."""
        for java, major in self.javas():
            for need, ver, tgz in JDTLS:
                if major >= need:
                    return java, major, ver, tgz
        best = self.javas()[0] if self.javas() else (None, 0)
        return None, best[1], None, None

    def jdtls_dir(self, ver):
        return os.path.join(self.base, "jdtls-" + ver)

    def debug_jar(self):
        return os.path.join(self.base, "java-debug-%s.jar" % DEBUG_VERSION)

    def installed(self):
        java, _major, ver, _tgz = self.runtime()
        return bool(java and glob.glob(os.path.join(self.jdtls_dir(ver), "plugins", "org.eclipse.equinox.launcher_*.jar")))

    def status(self):
        java, major, ver, _tgz = self.runtime()
        javas = self.javas()
        return {"java": java, "java_version": major, "javas": [{"path": p, "version": v} for p, v in javas],
                "jdtls": ver, "installed": self.installed(), "debugger": os.path.isfile(self.debug_jar()),
                "busy": self.busy, "error": self.error,
                "problem": "" if java else ("Java %d was found, but the Java tools need Java 17 or newer to run "
                                            "(your projects can still use Java %d). Install a JDK 21 and set it in Settings."
                                            % (major, major) if major else "No Java was found. Install a JDK (21 is best) "
                                            "or set where it is in Settings › Java.")}

    def _download(self, url, dest, label):
        tmp = dest + ".part"
        with net.open_url(url, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 0.4:
                        last = time.time()
                        self.publish({"type": "java_setup", "label": label, "done": done, "total": total})
        os.replace(tmp, dest)

    def install(self):
        """Download what's missing (on a worker thread)."""
        if self.busy:
            return
        java, _major, ver, tgz = self.runtime()
        if not java:
            self.error = self.status()["problem"]
            self.publish({"type": "java_setup", "error": self.error})
            return
        self.busy, self.error = True, ""

        def work():
            try:
                os.makedirs(self.base, exist_ok=True)
                target = self.jdtls_dir(ver)
                if not self.installed():
                    archive = os.path.join(self.base, tgz)
                    self._download(JDTLS_URL % (ver, tgz), archive, "Java language server")
                    self.publish({"type": "java_setup", "label": "Unpacking…"})
                    shutil.rmtree(target, ignore_errors=True)
                    with tarfile.open(archive) as t:
                        for m in t.getmembers():       # only plain files and folders, only inside the target
                            if not (m.isfile() or m.isdir()) or m.name.startswith(("/", "..")) or "/../" in m.name:
                                raise ValueError("Unexpected file in the download: %s" % m.name)
                        t.extractall(target)
                    os.remove(archive)
                if not os.path.isfile(self.debug_jar()):
                    self._download(DEBUG_URL, self.debug_jar(), "Java debugger")
                self.publish({"type": "java_setup", "ok": True})
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.error = "The download failed: %s. If your company blocks downloads, ask IT to allow " \
                             "download.eclipse.org and repo1.maven.org." % e
                self.publish({"type": "java_setup", "error": self.error})
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()


# ── NetBeans Ant projects: tell the server where the sources and libraries are ─
def read_properties(path):
    """A Java .properties file as a dict ({} if it can't be read)."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    props = {}
    text = re.sub(r"(?<!\\)\\\r?\n\s*", "", text)          # joined continuation lines
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "!")) and re.search(r"[=:]", line):
            k, v = re.split(r"\s*(?<!\\)[=:]\s*", line, maxsplit=1)
            props[k.strip()] = re.sub(r"\\(.)", r"\1", v.strip())
    return props


def split_classpath(value):
    """Entries of a classpath joined with ':' or ';', keeping Windows drive letters (C:\\lib\\a.jar) whole."""
    out = []
    for part in re.split(r"[:;]", value):
        part = part.strip()
        if out and len(out[-1]) == 1 and out[-1].isalpha() and part[:1] in ("\\", "/"):
            out[-1] += ":" + part
        elif part:
            out.append(part)
    return out


def ant_project_settings(root):
    """Source folders and jars of a NetBeans Ant project (nbproject/project.properties), for an 'invisible' project.

    NetBeans' own libraries (${libs.junit_4.classpath} and the like) live in the IDE's build.properties, which
    nbproject/private/private.properties points to; it's read too when it's there."""
    if not os.path.isfile(os.path.join(root, "nbproject", "project.properties")):
        return None
    private = read_properties(os.path.join(root, "nbproject", "private", "private.properties"))
    props = read_properties(private["user.properties.file"]) if private.get("user.properties.file") else {}
    props.update(private)
    props.update(read_properties(os.path.join(root, "nbproject", "project.properties")))

    def expand(v, depth=0):
        return re.sub(r"\$\{([^}]+)\}", lambda m: expand(props.get(m.group(1), ""), depth + 1) if depth < 10 else "", v)
    sources = [expand(v) for k, v in props.items() if re.fullmatch(r"(src|test)\.([\w.-]+\.)?dir", k)] or ["src", "test"]
    libs = []
    for k, v in props.items():
        if k.startswith("file.reference.") or k in ("javac.classpath", "javac.test.classpath", "run.classpath"):
            for part in split_classpath(expand(v)):
                if part.endswith(".jar"):
                    libs.append(part if os.path.isabs(part) else part.replace("\\", "/"))
    libs += ["lib/**/*.jar"]
    return {"sourcePaths": sorted({s.replace("\\", "/") for s in sources if s and os.path.isdir(os.path.join(root, s))}),
            "referencedLibraries": sorted(set(libs))}


def server_settings(root, extra=None):
    java = {
        # generatesMetadataFilesAtProjectRoot: keep Eclipse's .project/.classpath/.settings out of the project (and Git)
        "import": {"generatesMetadataFilesAtProjectRoot": False, "maven": {"enabled": True}, "gradle": {"enabled": True}, "exclusions": ["**/node_modules/**", "**/.metadata/**", "**/archetype-resources/**", "**/META-INF/maven/**"]},
        "configuration": {"updateBuildConfiguration": "automatic"},
        "autobuild": {"enabled": True}, "maxConcurrentBuilds": 1,
        "completion": {"enabled": True, "guessMethodArguments": "insertBestGuessedArguments", "importOrder": ["java", "javax", "org", "com", ""],
                       "favoriteStaticMembers": ["org.junit.jupiter.api.Assertions.*", "org.junit.Assert.*", "org.mockito.Mockito.*"]},
        "signatureHelp": {"enabled": True, "description": {"enabled": True}}, "format": {"enabled": True},
        "saveActions": {"organizeImports": False}, "referencesCodeLens": {"enabled": False}, "implementationsCodeLens": {"enabled": False},
        "contentProvider": {"preferred": "fernflower"}, "sources": {"organizeImports": {"starThreshold": 99, "staticStarThreshold": 99}},
        "errors": {"incompleteClasspath": {"severity": "warning"}},
    }
    m2 = os.path.join(os.path.expanduser("~"), ".m2", "settings.xml")
    if os.path.isfile(m2):
        java["configuration"]["maven"] = {"userSettings": m2}
    has_build = any(os.path.isfile(os.path.join(root, f)) for f in ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", ".classpath"))
    if not has_build:
        ant = ant_project_settings(root)
        if ant:
            java["project"] = ant
    if extra:
        java.update(extra)
    return {"java": java}


SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class Server:
    """The language server for one project."""

    def __init__(self, tools, project_id, root, work_dir, publish):
        self.tools, self.id, self.root = tools, project_id, os.path.realpath(root)
        self.work_dir, self.publish = work_dir, publish
        self.proc, self.lsp = None, None
        self.state, self.message = "stopped", ""
        self.diagnostics = {}            # uri -> [diagnostic]
        self.open_docs = {}              # uri -> version
        self.settings = server_settings(self.root)
        self.stderr_tail = []
        self.watch_stop = threading.Event()
        self.lock = threading.Lock()

    # -- lifecycle --
    def start(self):
        with self.lock:
            if self.state in ("starting", "importing", "ready"):
                return
            java, _major, ver, _tgz = self.tools.runtime()
            if not java or not self.tools.installed():
                raise LspError("Set up Java support first (Projects › Java › Set up).")
            home = self.tools.jdtls_dir(ver)
            launcher = glob.glob(os.path.join(home, "plugins", "org.eclipse.equinox.launcher_*.jar"))[0]
            cfg = {"Linux": "config_linux", "Darwin": "config_mac", "Windows": "config_win"}.get(platform.system(), "config_linux")
            if platform.machine().lower() in ("arm64", "aarch64") and os.path.isdir(os.path.join(home, cfg + "_arm")):
                cfg += "_arm"
            os.makedirs(self.work_dir, exist_ok=True)
            cmd = [java, "-Declipse.application=org.eclipse.jdt.ls.core.id1", "-Dosgi.bundles.defaultStartLevel=4",
                   "-Declipse.product=org.eclipse.jdt.ls.core.product", "-Dosgi.checkConfiguration=true",
                   "-Dosgi.sharedConfiguration.area=" + os.path.join(home, cfg), "-Dosgi.sharedConfiguration.area.readOnly=true",
                   "-Dosgi.configuration.cascaded=true", "-Dlog.level=WARNING",
                   # keep Eclipse's .project/.classpath/.settings in the app's data folder, out of the project and Git
                   "-Djava.import.generatesMetadataFilesAtProjectRoot=false", "-Xms128m", "-Xmx%s" % (self.tools.settings.get("memory") or "1G"),
                   "-XX:+UseParallelGC", "-XX:GCTimeRatio=4", "-XX:AdaptiveSizePolicyWeight=90", "-Dsun.zip.disableMemoryMapping=true",
                   "--add-modules=ALL-SYSTEM", "--add-opens", "java.base/java.util=ALL-UNNAMED", "--add-opens", "java.base/java.lang=ALL-UNNAMED",
                   "-jar", launcher, "-configuration", os.path.join(self.work_dir, "config"), "-data", os.path.join(self.work_dir, "data")]
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=self.root, creationflags=flags)
            self.lsp = Lsp(self.proc, self._notify, self._server_request)
            self._set("starting", "Starting the Java language server…")
            threading.Thread(target=self._stderr, daemon=True).start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def _stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").rstrip()
            if text and not text.startswith("Picked up JAVA_TOOL_OPTIONS"):
                self.stderr_tail = (self.stderr_tail + [text])[-30:]
        code = self.proc.wait()
        if self.state != "stopped":
            self._set("error", "The Java language server stopped (exit code %s). %s" % (code, " ".join(self.stderr_tail[-3:])[:400]))

    def _initialize(self):
        bundles = [self.tools.debug_jar()] if os.path.isfile(self.tools.debug_jar()) else []
        root_uri = file_uri(self.root)
        caps = {
            "workspace": {"applyEdit": True, "workspaceEdit": {"documentChanges": True, "resourceOperations": ["create", "rename", "delete"]},
                          "configuration": True, "didChangeConfiguration": {"dynamicRegistration": True},
                          "didChangeWatchedFiles": {"dynamicRegistration": True}, "executeCommand": {"dynamicRegistration": True},
                          "workspaceFolders": True, "symbol": {"dynamicRegistration": True}},
            "textDocument": {
                "synchronization": {"didSave": True, "dynamicRegistration": True},
                "completion": {"contextSupport": True, "completionItem": {
                    "snippetSupport": True, "documentationFormat": ["markdown", "plaintext"], "deprecatedSupport": True,
                    "labelDetailsSupport": True, "tagSupport": {"valueSet": [1]},
                    "resolveSupport": {"properties": ["documentation", "detail", "additionalTextEdits"]}},
                    "completionItemKind": {"valueSet": list(range(1, 26))}},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "signatureHelp": {"signatureInformation": {"documentationFormat": ["markdown", "plaintext"],
                                                          "parameterInformation": {"labelOffsetSupport": True}, "activeParameterSupport": True}},
                "definition": {"linkSupport": False}, "typeDefinition": {}, "implementation": {}, "references": {},
                "documentHighlight": {}, "documentSymbol": {"hierarchicalDocumentSymbolSupport": True, "symbolKind": {"valueSet": list(range(1, 27))}},
                "formatting": {}, "rangeFormatting": {}, "onTypeFormatting": {}, "rename": {"prepareSupport": True},
                "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {"valueSet": [
                    "", "quickfix", "refactor", "refactor.extract", "refactor.inline", "refactor.rewrite", "source", "source.organizeImports"]}},
                    "dataSupport": True, "resolveSupport": {"properties": ["edit"]}, "isPreferredSupport": True},
                "publishDiagnostics": {"relatedInformation": True, "tagSupport": {"valueSet": [1, 2]}, "versionSupport": False},
                "foldingRange": {"lineFoldingOnly": True}, "selectionRange": {},
            },
            "window": {"workDoneProgress": True, "showMessage": {}},
        }
        init_opts = {
            "bundles": bundles, "workspaceFolders": [root_uri], "settings": self.settings,
            "extendedClientCapabilities": {"classFileContentsSupport": True, "progressReportProvider": True,
                                           "resolveAdditionalTextEditsSupport": True, "actionableRuntimeNotificationSupport": False,
                                           "overrideMethodsPromptSupport": False, "hashCodeEqualsPromptSupport": False,
                                           "advancedOrganizeImportsSupport": False, "generateToStringPromptSupport": False,
                                           "advancedGenerateAccessorsSupport": False, "generateConstructorsPromptSupport": False,
                                           "generateDelegateMethodsPromptSupport": False, "advancedExtractRefactoringSupport": False,
                                           "inferSelectionSupport": [], "moveRefactoringSupport": False, "clientHoverProvider": True,
                                           "clientDocumentSymbolProvider": True, "gradleChecksumWrapperPromptSupport": False,
                                           "skipProjectConfiguration": False},
            "triggerFiles": [],
        }
        try:
            self.lsp.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "VSCode Assistant"}, "rootUri": root_uri, "rootPath": self.root,
                "capabilities": caps, "initializationOptions": init_opts, "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root)}],
            }, timeout=180)
            self.lsp.notify("initialized", {})
            self.lsp.notify("workspace/didChangeConfiguration", {"settings": self.settings})
            if self.state == "starting":
                self._set("importing", "Reading the project…")
            threading.Thread(target=self._watch, daemon=True).start()
        except LspError as e:
            self._set("error", "The Java language server didn't start: %s %s" % (e, " ".join(self.stderr_tail[-3:])[:400]))

    def stop(self):
        with self.lock:
            self.watch_stop.set()
            if not self.proc:
                return
            prev, self.state = self.state, "stopped"
            try:
                if self.lsp and not self.lsp.closed and prev == "ready":
                    self.lsp.request("shutdown", None, timeout=5)
                    self.lsp.notify("exit", None)
            except LspError:
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        self._set("stopped", "")

    def _set(self, state, message):
        self.state, self.message = state, message
        self.publish({"type": "java", "project": self.id, "kind": "status", "state": state, "message": message})

    # -- what the server tells us --
    def _notify(self, method, params):
        params = params or {}
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            items = params.get("diagnostics") or []
            if items:
                self.diagnostics[uri] = items
            else:
                self.diagnostics.pop(uri, None)
            self.publish({"type": "java", "project": self.id, "kind": "diagnostics", "uri": uri, "items": items})
        elif method == "language/status":
            t, msg = params.get("type"), params.get("message") or ""
            if t == "ServiceReady":
                self._set("ready", "Ready")
            elif t == "Error":
                self._set("error", msg)
            elif t in ("Starting", "ProjectStatus", "Message") and self.state != "ready":
                msg = re.sub(r"^\d+%\s*", "", msg).replace("Starting Java Language Server - ", "").strip()
                if msg and msg not in ("OK", "Init..."):
                    self._set("importing", msg[:160])
        elif method == "language/progressReport":
            if self.state != "error":
                self.publish({"type": "java", "project": self.id, "kind": "progress", "task": params.get("task") or "",
                              "sub": params.get("subTask") or "", "done": bool(params.get("complete")),
                              "work": params.get("workDone"), "total": params.get("totalWork")})
        elif method in ("window/showMessage", "language/actionableNotification"):
            if (params.get("type") or 3) <= 2:
                self.publish({"type": "java", "project": self.id, "kind": "message", "level": params.get("severity") or params.get("type"),
                              "message": params.get("message") or ""})
        elif method == "$/progress":
            v = params.get("value") or {}
            if v.get("kind") in ("begin", "report") and self.state != "ready":
                self.publish({"type": "java", "project": self.id, "kind": "progress", "task": v.get("title") or "", "sub": v.get("message") or "",
                              "done": False, "work": v.get("percentage"), "total": 100 if v.get("percentage") is not None else None})

    def _server_request(self, method, params):
        if method == "workspace/configuration":
            out = []
            for item in (params or {}).get("items") or []:
                node = self.settings
                for part in (item.get("section") or "").split("."):
                    node = node.get(part) if isinstance(node, dict) and part else node
                out.append(node)
            return out
        if method == "workspace/applyEdit":
            changed = self.apply_edit((params or {}).get("edit") or {})
            return {"applied": True, "changed": changed}
        if method in ("client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create"):
            return None
        if method == "workspace/workspaceFolders":
            return [{"uri": file_uri(self.root), "name": os.path.basename(self.root)}]
        if method == "window/showMessageRequest":
            return None
        if method == "java/checkProjectSettingsConfiguration":
            return {}
        return None

    # -- files --
    def inside(self, uri):
        p = uri_path(uri)
        if p is None:
            return None
        real = os.path.realpath(p)
        return real if real == self.root or real.startswith(self.root + os.sep) else None

    def apply_edit(self, edit, from_page=False):
        """Apply a WorkspaceEdit to the files on disk; files open in the page get it there instead (see the event).
        from_page: the page already applied the parts for its open files."""
        changes = []
        for uri, edits in (edit.get("changes") or {}).items():
            changes.append(("edit", uri, edits))
        for dc in edit.get("documentChanges") or []:
            kind = dc.get("kind")
            if kind == "create":
                changes.append(("create", dc["uri"], None))
            elif kind == "rename":
                changes.append(("rename", dc["oldUri"], dc["newUri"]))
            elif kind == "delete":
                changes.append(("delete", dc["uri"], None))
            elif "textDocument" in dc:
                changes.append(("edit", dc["textDocument"]["uri"], dc.get("edits") or []))
        done = []
        for kind, uri, extra in changes:
            path = self.inside(uri)
            if not path:
                continue
            if kind == "edit" and uri in self.open_docs:
                if not from_page:
                    done.append({"uri": uri, "open": True, "edits": extra})      # the page applies it to its editor
                continue
            if kind == "edit":
                try:
                    text = open(path, encoding="utf-8", newline="").read()
                except OSError:
                    continue
                text = apply_text_edits(text, extra)
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                done.append({"uri": uri})
            elif kind == "create":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if not os.path.exists(path):
                    open(path, "w", encoding="utf-8").close()
                done.append({"uri": uri, "created": True})
            elif kind == "rename":
                new = self.inside(extra)
                if new and os.path.exists(path):
                    os.makedirs(os.path.dirname(new), exist_ok=True)
                    os.replace(path, new)
                    done.append({"uri": uri, "renamed": extra})
            elif kind == "delete" and os.path.isfile(path):
                os.remove(path)
                done.append({"uri": uri, "deleted": True})
        if done:
            self.publish({"type": "java", "project": self.id, "kind": "edits", "changes": done})
        return done

    def _watch(self):
        """Report file changes the editor didn't make (the AI, Git, a build) so the server's view stays right."""
        def scan():
            seen = {}
            for dirpath, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for f in files:
                    if WATCH.match(f):
                        p = os.path.join(dirpath, f)
                        try:
                            seen[p] = os.stat(p).st_mtime_ns
                        except OSError:
                            pass
                if len(seen) > 50000:
                    break
            return seen
        before = scan()
        while not self.watch_stop.wait(2.5):
            if self.state not in ("ready", "importing"):
                continue
            now = scan()
            events = [{"uri": file_uri(p), "type": 1} for p in now.keys() - before.keys()]
            events += [{"uri": file_uri(p), "type": 3} for p in before.keys() - now.keys()]
            events += [{"uri": file_uri(p), "type": 2} for p in now.keys() & before.keys() if now[p] != before[p]]
            before = now
            events = [e for e in events if e["uri"] not in self.open_docs]
            if events:
                try:
                    self.lsp.notify("workspace/didChangeWatchedFiles", {"changes": events[:2000]})
                except LspError:
                    return

    # -- what the page asks --
    def sync(self, action, uri, text=None, version=None):
        if self.state in ("stopped", "error") or not self.lsp:
            return
        if action == "reset":            # a page (re)loaded: forget the files an earlier page had open
            for u in list(self.open_docs):
                self.open_docs.pop(u, None)
                self.lsp.notify("textDocument/didClose", {"textDocument": {"uri": u}})
            return
        if not self.inside(uri):
            raise LspError("That file isn't in this project.")
        if action == "open":
            if uri in self.open_docs:    # already open (another tab or an earlier page): start from this copy
                self.lsp.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            self.open_docs[uri] = version or 1
            self.lsp.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "java", "version": self.open_docs[uri], "text": text}})
        elif action == "change" and uri in self.open_docs:
            self.open_docs[uri] = max(self.open_docs[uri] + 1, version or 0)
            self.lsp.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": self.open_docs[uri]}, "contentChanges": [{"text": text}]})
        elif action == "save" and uri in self.open_docs:
            self.lsp.notify("textDocument/didSave", {"textDocument": {"uri": uri}, "text": text})
        elif action == "close" and uri in self.open_docs:
            self.open_docs.pop(uri, None)
            self.lsp.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def call(self, method, params, timeout=30):
        if method not in PAGE_METHODS:
            raise LspError("Not allowed: %s" % method)
        if method == "workspace/executeCommand" and (params or {}).get("command") not in PAGE_COMMANDS:
            raise LspError("Not allowed: %s" % (params or {}).get("command"))
        if not self.lsp or self.state in ("stopped", "error"):
            raise LspError("The Java language server isn't running.")
        doc = ((params or {}).get("textDocument") or {}).get("uri")
        if doc and not doc.startswith("jdt://") and not self.inside(doc):
            raise LspError("That file isn't in this project.")
        return self.lsp.request(method, params, timeout)

    def execute(self, command, args, timeout=60):
        """Commands the app itself runs (the debugger's), not offered to the page."""
        return self.lsp.request("workspace/executeCommand", {"command": command, "arguments": args or []}, timeout)

    def info(self):
        counts = {"error": 0, "warning": 0}
        for items in self.diagnostics.values():
            for d in items:
                sev = SEVERITY.get(d.get("severity") or 1, "error")
                if sev in counts:
                    counts[sev] += 1
        return {"state": self.state, "message": self.message, "root_uri": file_uri(self.root), "counts": counts,
                "diagnostics": [{"uri": u, "items": i} for u, i in self.diagnostics.items()]}


def apply_text_edits(text, edits):
    """Apply LSP TextEdits (line/character ranges) to a string."""
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1

    def offset(p):
        ln = min(p["line"], len(lines) - 1) if lines else 0
        if p["line"] >= len(lines):
            return len(text)
        return starts[ln] + min(p["character"], len(lines[ln]))
    spans = sorted(((offset(e["range"]["start"]), offset(e["range"]["end"]), e.get("newText") or "") for e in edits),
                   key=lambda s: (s[0], s[1]), reverse=True)
    for a, b, new in spans:
        text = text[:a] + new + text[b:]
    return text


class Java:
    """All of it, for the Hub: the tools and one server per project."""

    def __init__(self, data_dir, settings, publish):
        self.data_dir, self.publish = data_dir, publish
        self.tools = Tools(data_dir, settings, publish)
        self.servers = {}

    def server(self, project_id, root, start=True):
        s = self.servers.get(project_id)
        if s and os.path.realpath(root) != s.root:
            s.stop()
            s = None
        if not s:
            s = self.servers[project_id] = Server(self.tools, project_id, root, os.path.join(self.data_dir, "java", project_id), self.publish)
        if start and s.state in ("stopped", "error"):
            s.start()
        return s

    def stop_all(self):
        for s in list(self.servers.values()):
            try:
                s.stop()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass

    @staticmethod
    def is_java_project(root):
        if any(os.path.isfile(os.path.join(root, f)) for f in ("pom.xml", "build.gradle", "build.gradle.kts", ".classpath")):
            return True
        if os.path.isfile(os.path.join(root, "nbproject", "project.properties")):
            return True
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if any(f.endswith(".java") for f in files):
                return True
            if dirpath.count(os.sep) - root.count(os.sep) > 4:
                dirs[:] = []
        return False

    def test_command(self, root, cls, method=None, debug_port=None):
        """The command that runs one test class or method, as NetBeans would (Maven, Gradle or NetBeans Ant)."""
        if os.path.isfile(os.path.join(root, "pom.xml")):
            sel = cls.split(".")[-1] + ("#" + method if method else "")
            cmd = "%s test -Dtest=%s -Dsurefire.failIfNoSpecifiedTests=false" % (testlog.mvn(root), sel)
            if debug_port:
                cmd += ' -Dmaven.surefire.debug="-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=localhost:%d"' % debug_port
            return cmd
        if any(os.path.isfile(os.path.join(root, f)) for f in ("build.gradle", "build.gradle.kts")):
            g = ("gradlew.bat" if sys.platform == "win32" else "./gradlew") if os.path.exists(os.path.join(root, "gradlew")) else "gradle"
            cmd = "%s test --tests %s" % (g, cls + ("." + method if method else ""))
            if debug_port:
                cmd += " --debug-jvm"
            return cmd
        if os.path.isfile(os.path.join(root, "build.xml")):
            path = cls.replace(".", "/") + ".java"
            cmd = '%s -Djavac.includes=%s -Dtest.includes=%s' % (testlog.ant(), path, path)
            if method:
                cmd += " -Dtest.class=%s -Dtest.method=%s test-single-method" % (cls, method)
            else:
                cmd += " test-single"
            if debug_port:
                cmd += ' -Djvm.args="-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=localhost:%d"' % debug_port
            return cmd
        return None
