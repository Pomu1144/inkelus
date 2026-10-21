"""TypeScript and JavaScript (React too) for Projects, through typescript-language-server.

It wraps TypeScript's own language service (tsserver), the one VS Code uses, so errors and completions match
what tsc and the project's build say: it reads the project's tsconfig.json and node_modules, and uses the
project's own TypeScript when it has one. ESLint runs next to it (vscode-eslint-language-server) with the
project's own ESLint and config, and Prettier formats with the project's Prettier when it has one.

Nothing is bundled: the first time, "Set up" runs npm to install the language servers (and a TypeScript to
fall back on) into the app's data folder. It needs Node.js 18 or newer on the computer.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

from . import javals
from .javals import LspError, file_uri

TS_FALLBACK = "5.9.3"            # used when the project has no TypeScript of its own
ESLINT_LS = "4.10.0"             # vscode-langservers-extracted

TS_METHODS = (javals.PAGE_METHODS - {"java/classFileContents"}) | {"textDocument/inlayHint"}
TS_COMMANDS = {"_typescript.applyWorkspaceEdit", "_typescript.applyCodeAction", "_typescript.applyRefactoring",
               "_typescript.organizeImports", "_typescript.applyRenameFile", "_typescript.goToSourceDefinition"}
ESLINT_METHODS = {"textDocument/codeAction", "workspace/executeCommand"}
ESLINT_COMMANDS = {"eslint.applyAllFixes", "eslint.applySingleFix", "eslint.applySuggestion", "eslint.applySameFixes",
                   "eslint.applyDisableLine", "eslint.applyDisableFile"}
SOURCE = re.compile(r".*\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$", re.I)
CONFIG = re.compile(r"(tsconfig.*|jsconfig.*|package)\.json$|.*\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$", re.I)
LANGUAGE_IDS = {"ts": "typescript", "mts": "typescript", "cts": "typescript", "tsx": "typescriptreact",
                "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "javascriptreact"}


def node_version(node, full=False):
    """Major version of a node executable (or (major, minor, patch) with full=True); 0 if it doesn't run."""
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return (0, 0, 0) if full else 0
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return (0, 0, 0) if full else 0
    return tuple(int(x) for x in m.groups()) if full else int(m.group(1))


def find_node(extra=""):
    """(node executable, major version) or (None, 0)."""
    exe = "node.exe" if sys.platform == "win32" else "node"
    cands = []
    if extra:
        cands.append(os.path.join(extra, exe) if os.path.isdir(extra) else extra)
    if shutil.which("node"):
        cands.append(shutil.which("node"))
    if sys.platform == "win32":
        cands += [os.path.join(os.environ.get(v, ""), "nodejs", exe) for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
    for c in cands:
        if os.path.isfile(c):
            v = node_version(c)
            if v:
                return c, v
    return None, 0


def project_package(root):
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            pkg = json.load(f)
        return pkg if isinstance(pkg, dict) else {}
    except (OSError, ValueError):
        return {}


def local_bin(root, *parts):
    """A file in the project's node_modules (or a parent folder's, for monorepos), or None."""
    d = os.path.realpath(root)
    for _ in range(6):
        p = os.path.join(d, "node_modules", *parts)
        if os.path.exists(p):
            return p
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return None


class Tools:
    def __init__(self, data_dir, settings, publish):
        self.base = os.path.join(data_dir, "tools", "node-ls")
        self.settings = settings        # the "ts" section of the app's settings
        self.publish = publish
        self.busy, self.error = False, ""
        self._node = None

    def node(self, refresh=False):
        if self._node is None or refresh:
            self._node = find_node(self.settings.get("node_path") or "")
        return self._node

    def angular_version(self):
        """The newest Angular language server this Node can run (it loads the project's own Angular language
        service when the project has one, so it suits older Angular versions too)."""
        v = node_version(self.node()[0], full=True) if self.node()[0] else (0, 0, 0)
        if v >= (22, 22, 3):
            return "22.2.3"
        if v >= (20, 19, 0):
            return "21.2.25"
        return "19.2.4"

    def ng_cli(self):
        p = os.path.join(self.base, "node_modules", "@angular", "language-server", "index.js")
        return p if os.path.isfile(p) else None

    def server_version(self):
        # typescript-language-server 5 needs Node 20; 4.4 still runs on Node 18
        return "5.3.0" if self.node()[1] >= 20 else "4.4.1"

    def _cli(self, package):
        """The entry script of an installed npm package's command (run with node directly, no .cmd shims)."""
        d = os.path.join(self.base, "node_modules", package)
        try:
            with open(os.path.join(d, "package.json"), encoding="utf-8") as f:
                b = json.load(f).get("bin")
        except (OSError, ValueError):
            return None
        if isinstance(b, dict):
            b = b.get("typescript-language-server") or b.get("vscode-eslint-language-server") or next(iter(b.values()), None)
        return os.path.join(d, b) if b and os.path.isfile(os.path.join(d, b)) else None

    def ts_cli(self):
        return self._cli("typescript-language-server")

    def eslint_cli(self):
        p = os.path.join(self.base, "node_modules", "vscode-langservers-extracted", "bin", "vscode-eslint-language-server")
        return p if os.path.isfile(p) else None

    def fallback_tsserver(self):
        p = os.path.join(self.base, "node_modules", "typescript", "lib", "tsserver.js")
        return p if os.path.isfile(p) else None

    def installed(self):
        return bool(self.node()[0] and self.ts_cli() and self.fallback_tsserver())

    def status(self):
        node, major = self.node()
        problem = ""
        if not node:
            problem = "Node.js wasn't found. Install Node.js 18 or newer (or set where it is in Settings › TypeScript)."
        elif major < 18:
            problem = "Node.js %d was found, but the TypeScript tools need Node.js 18 or newer." % major
        return {"node": node, "node_version": major, "installed": self.installed(), "eslint": bool(self.eslint_cli()), "angular": bool(self.ng_cli()),
                "server": self.server_version(), "busy": self.busy, "error": self.error, "problem": problem}

    def install(self):
        """npm install the language servers into the app's data folder (in the background; reports java_setup events)."""
        node, major = self.node(refresh=True)
        if self.busy or not node or major < 18:
            return
        npm = shutil.which("npm") or (os.path.join(os.path.dirname(node), "npm.cmd" if sys.platform == "win32" else "npm"))
        self.busy, self.error = True, ""

        def run():
            try:
                os.makedirs(self.base, exist_ok=True)
                pkg = os.path.join(self.base, "package.json")
                if not os.path.exists(pkg):
                    with open(pkg, "w", encoding="utf-8") as f:
                        json.dump({"name": "vscode-assistant-node-tools", "private": True}, f)
                self.publish({"type": "java_setup", "lang": "ts", "label": "Installing the TypeScript tools with npm…", "done": 0, "total": 0})
                out = subprocess.run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error", "--prefix", self.base,
                                      "typescript-language-server@" + self.server_version(), "typescript@" + TS_FALLBACK,
                                      "vscode-langservers-extracted@" + ESLINT_LS, "@angular/language-server@" + self.angular_version()],
                                     capture_output=True, text=True, timeout=900, cwd=self.base,
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                if out.returncode != 0:
                    raise OSError((out.stderr or out.stdout or "npm failed").strip().splitlines()[-1][:300])
                if not self.installed():
                    raise OSError("npm finished, but the language server isn't where it should be.")
            except (OSError, subprocess.SubprocessError) as e:
                self.error = "Installing the TypeScript tools failed: %s" % e
            finally:
                self.busy = False
                self.publish({"type": "java_setup", "lang": "ts", "done": 1, "total": 1, "finished": True, "error": self.error})
        threading.Thread(target=run, daemon=True).start()


class TsServer(javals.Server):
    """typescript-language-server for one project."""
    LANG, LABEL = "ts", "TypeScript"
    PAGE_METHODS, PAGE_COMMANDS, WATCH = TS_METHODS, TS_COMMANDS, CONFIG

    def __init__(self, tools, project_id, root, work_dir, publish):
        super().__init__(tools, project_id, root, work_dir, publish)
        self.settings = self.ts_settings()
        self.ts_version = ""

    def ts_settings(self):
        prefs = {"quoteStyle": "auto", "importModuleSpecifier": "shortest", "includeCompletionsForModuleExports": True,
                 "includeCompletionsForImportStatements": True, "includeCompletionsWithSnippetText": True,
                 "includeAutomaticOptionalChainCompletions": True, "includeCompletionsWithInsertText": True,
                 "jsxAttributeCompletionStyle": "auto", "providePrefixAndSuffixTextForRename": True, "allowRenameOfImportPath": True}
        lang = {"preferences": prefs, "format": {"semicolons": "ignore"}, "suggest": {"completeFunctionCalls": False},
                "implicitProjectConfiguration": {"checkJs": False}}
        return {"typescript": lang, "javascript": lang, "completions": {"completeFunctionCalls": False}, "diagnostics": {"ignoredCodes": []}}

    def tsserver(self):
        """The project's own TypeScript when it has one with tsserver (TypeScript 7 has none), else ours."""
        own = local_bin(self.root, "typescript", "lib", "tsserver.js")
        return own or self.tools.fallback_tsserver()

    def language_id(self, uri):
        return LANGUAGE_IDS.get(uri.rsplit(".", 1)[-1].lower(), "typescript")

    def start(self):
        with self.lock:
            if self.state in ("starting", "importing", "ready"):
                return
            node, _major = self.tools.node()
            cli = self.tools.ts_cli()
            if not node or not cli:
                raise LspError("Set up TypeScript support first.")
            cmd = [node, cli, "--stdio"]
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=self.root, creationflags=flags)
            self.lsp = javals.Lsp(self.proc, self._notify, self._server_request)
            self._set("starting", "Starting the TypeScript language server…")
            threading.Thread(target=self._stderr, daemon=True).start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def _initialize(self):
        root_uri = file_uri(self.root)
        tsserver = self.tsserver()
        opts = {"hostInfo": "vscode-assistant", "tsserver": {"path": tsserver, "logVerbosity": "off"},
                "preferences": self.settings["typescript"]["preferences"], "maxTsServerMemory": 3072,
                "disableAutomaticTypingAcquisition": True}
        try:
            self.lsp.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "VSCode Assistant"}, "rootUri": root_uri, "rootPath": self.root,
                "capabilities": self.capabilities(), "initializationOptions": opts,
                "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root)}],
            }, timeout=120)
            self.lsp.notify("initialized", {})
            self.lsp.notify("workspace/didChangeConfiguration", {"settings": self.settings})
            own = bool(tsserver) and not tsserver.startswith(self.tools.base)
            self._set("ready", "Ready" if own else "Ready (the project has no TypeScript of its own; using %s)" % TS_FALLBACK)
            threading.Thread(target=self._watch, daemon=True).start()
        except LspError as e:
            self._set("error", "The TypeScript language server didn't start: %s %s" % (e, " ".join(self.stderr_tail[-3:])[:400]))

    def _notify(self, method, params):
        if method == "$/typescriptVersion":
            self.ts_version = (params or {}).get("version") or ""
            return
        super()._notify(method, params)

    def _server_request(self, method, params):
        if method == "workspace/configuration":
            return [self.settings.get((i.get("section") or "").split(".")[0]) for i in (params or {}).get("items") or []]
        return super()._server_request(method, params)

    def info(self):
        out = super().info()
        out["ts_version"] = self.ts_version
        return out


class EslintServer(TsServer):
    """ESLint's warnings and fixes, with the project's own ESLint and config (like VS Code's ESLint extension)."""
    LANG, LABEL = "eslint", "ESLint"
    PAGE_METHODS, PAGE_COMMANDS, WATCH = ESLINT_METHODS, ESLINT_COMMANDS, re.compile(r"(\.eslintrc.*|eslint\.config\..*|package\.json)$")

    def eslint_settings(self, scope_uri=None):
        flat = any(os.path.exists(os.path.join(self.root, "eslint.config." + e)) for e in ("js", "mjs", "cjs", "ts", "mts", "cts"))
        return {"validate": "on", "packageManager": "npm", "useESLintClass": False, "useFlatConfig": flat,
                "experimental": {"useFlatConfig": flat}, "codeAction": {"disableRuleComment": {"enable": True, "location": "separateLine"},
                                                                        "showDocumentation": {"enable": False}},
                "codeActionOnSave": {"enable": False, "mode": "all"}, "format": False, "quiet": False, "onIgnoredFiles": "off",
                "options": {}, "rulesCustomizations": [], "run": "onType", "problems": {"shortenToSingleLine": False},
                "nodePath": None, "workingDirectory": {"mode": "location"},
                "workspaceFolder": {"uri": file_uri(self.root), "name": os.path.basename(self.root)}}

    def start(self):
        with self.lock:
            if self.state in ("starting", "importing", "ready"):
                return
            node, _major = self.tools.node()
            cli = self.tools.eslint_cli()
            if not node or not cli:
                raise LspError("Set up TypeScript support first.")
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen([node, cli, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=self.root, creationflags=flags)
            self.lsp = javals.Lsp(self.proc, self._notify, self._server_request)
            self._set("starting", "Starting ESLint…")
            threading.Thread(target=self._stderr, daemon=True).start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def _initialize(self):
        root_uri = file_uri(self.root)
        try:
            self.lsp.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "VSCode Assistant"}, "rootUri": root_uri, "rootPath": self.root,
                "capabilities": self.capabilities(), "initializationOptions": {},
                "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root)}],
            }, timeout=60)
            self.lsp.notify("initialized", {})
            self.lsp.notify("workspace/didChangeConfiguration", {"settings": {}})     # it reads its settings only after this
            self._set("ready", "Ready")
            threading.Thread(target=self._watch, daemon=True).start()
        except LspError as e:
            self._set("error", "ESLint didn't start: %s %s" % (e, " ".join(self.stderr_tail[-3:])[:400]))

    def _notify(self, method, params):
        if method in ("eslint/status", "eslint/noLibrary", "eslint/noConfig"):
            if method != "eslint/status":
                self.message = "ESLint isn't set up in this project." if method == "eslint/noLibrary" else "No ESLint config found."
            return
        javals.Server._notify(self, method, params)

    # it reports problems when asked (pull diagnostics), so ask after each change and pass them on as usual
    def sync(self, action, uri, text=None, version=None):
        super().sync(action, uri, text, version)
        if action in ("open", "change", "save") and uri in self.open_docs:
            threading.Thread(target=self._pull, args=(uri, self.open_docs[uri]), daemon=True).start()

    def _pull(self, uri, version):
        try:
            r = self.lsp.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, timeout=30) or {}
        except LspError:
            return
        if self.open_docs.get(uri) == version and r.get("kind") == "full":
            javals.Server._notify(self, "textDocument/publishDiagnostics", {"uri": uri, "diagnostics": r.get("items") or []})

    def _server_request(self, method, params):
        if method == "workspace/configuration":
            return [self.eslint_settings() for _ in (params or {}).get("items") or []]
        if method == "workspace/diagnostic/refresh":
            for uri, version in list(self.open_docs.items()):
                threading.Thread(target=self._pull, args=(uri, version), daemon=True).start()
            return None
        if method in ("eslint/openDoc", "eslint/confirmESLintExecution", "eslint/probeFailed", "eslint/noLibrary", "eslint/noConfig"):
            return 4 if method == "eslint/confirmESLintExecution" else {}       # 4: approved
        return javals.Server._server_request(self, method, params)


class AngularServer(TsServer):
    """Angular's language server: templates (.html files and inline templates) and component errors, next to
    the TypeScript server. It uses the project's own @angular/language-service and TypeScript when it has them."""
    LANG, LABEL = "ng", "Angular"
    PAGE_METHODS = {"textDocument/completion", "textDocument/hover", "textDocument/signatureHelp", "textDocument/definition",
                    "textDocument/typeDefinition", "textDocument/references", "textDocument/prepareRename", "textDocument/rename",
                    "textDocument/codeAction", "codeAction/resolve"}
    PAGE_COMMANDS = set()
    WATCH = re.compile(r".*\.(ts|html)$|(tsconfig.*|angular)\.json$")

    def language_id(self, uri):
        return "html" if uri.lower().endswith(".html") else "typescript"

    def start(self):
        with self.lock:
            if self.state in ("starting", "importing", "ready"):
                return
            node, _major = self.tools.node()
            cli = self.tools.ng_cli()
            if not node or not cli:
                raise LspError("Set up Angular support first.")
            own = local_bin(self.root, "typescript", "package.json")
            ts_probe = [os.path.dirname(os.path.dirname(own))] if own else []
            ng_own = local_bin(self.root, "@angular", "language-service", "package.json")
            ng_probe = [os.path.dirname(os.path.dirname(os.path.dirname(ng_own)))] if ng_own else []
            mine = os.path.join(self.tools.base, "node_modules")
            cmd = [node, cli, "--stdio", "--tsProbeLocations", ",".join(ts_probe + [mine]),
                   "--ngProbeLocations", ",".join(ng_probe + [os.path.join(mine, "@angular", "language-server", "node_modules"), mine]),
                   "--includeCompletionsWithSnippetText", "--includeAutomaticOptionalChainCompletions"]
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=self.root, creationflags=flags)
            self.lsp = javals.Lsp(self.proc, self._notify, self._server_request)
            self._set("starting", "Starting the Angular language server…")
            threading.Thread(target=self._stderr, daemon=True).start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def _initialize(self):
        root_uri = file_uri(self.root)
        try:
            self.lsp.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "VSCode Assistant"}, "rootUri": root_uri, "rootPath": self.root,
                "capabilities": self.capabilities(), "initializationOptions": {},
                "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root)}],
            }, timeout=120)
            self.lsp.notify("initialized", {})
            self._set("ready", "Ready")
            threading.Thread(target=self._watch, daemon=True).start()
        except LspError as e:
            self._set("error", "The Angular language server didn't start: %s %s" % (e, " ".join(self.stderr_tail[-3:])[:400]))

    def _notify(self, method, params):
        if method == "angular/projectLoadingStart":
            self._set("ready", "Loading the Angular project…")
        elif method == "angular/projectLoadingFinish":
            self._set("ready", "Ready")
        elif method.startswith("angular/"):
            return
        else:
            javals.Server._notify(self, method, params)


class TypeScript:
    """All of it, for the Hub: the tools, and a TypeScript, an ESLint and (for Angular) an Angular server per project."""

    def __init__(self, data_dir, settings, publish):
        self.data_dir, self.publish = data_dir, publish
        self.tools = Tools(data_dir, settings, publish)
        self.servers, self.linters, self.angular = {}, {}, {}

    def server(self, project_id, root, start=True):
        s = self._get(self.servers, TsServer, project_id, root, start)
        if start and self.uses_eslint(root) and self.tools.eslint_cli():
            try:
                self._get(self.linters, EslintServer, project_id, root, True)
            except LspError:
                pass
        if start and self.is_angular(root) and self.tools.ng_cli():
            try:
                self._get(self.angular, AngularServer, project_id, root, True)
            except LspError:
                pass
        return s

    def _get(self, table, cls, project_id, root, start):
        s = table.get(project_id)
        if s and os.path.realpath(root) != s.root:
            s.stop()
            s = None
        if not s:
            s = table[project_id] = cls(self.tools, project_id, root, os.path.join(self.data_dir, "ts", project_id), self.publish)
        if start and s.state in ("stopped", "error"):
            s.start()
        return s

    def for_lang(self, lang, project_id):
        return {"eslint": self.linters, "ng": self.angular}.get(lang, self.servers).get(project_id)

    def stop(self, project_id):
        for table in (self.servers, self.linters, self.angular):
            s = table.pop(project_id, None)
            if s:
                s.stop()

    def stop_all(self):
        for table in (self.servers, self.linters, self.angular):
            for s in list(table.values()):
                try:
                    s.stop()
                except Exception:  # noqa: BLE001 - shutting down anyway
                    pass

    @staticmethod
    def is_ts_project(root):
        return any(os.path.isfile(os.path.join(root, f)) for f in ("tsconfig.json", "jsconfig.json", "package.json"))

    @staticmethod
    def uses_eslint(root):
        pkg = project_package(root)
        deps = dict(pkg.get("dependencies") or {}, **(pkg.get("devDependencies") or {}))
        return "eslint" in deps and bool(local_bin(root, "eslint", "package.json"))

    @staticmethod
    def is_angular(root):
        pkg = project_package(root)
        return "@angular/core" in dict(pkg.get("dependencies") or {}, **(pkg.get("devDependencies") or {})) or \
            os.path.isfile(os.path.join(root, "angular.json"))

    @staticmethod
    def test_runner(root):
        pkg = project_package(root)
        deps = dict(pkg.get("dependencies") or {}, **(pkg.get("devDependencies") or {}))
        for name in ("vitest", "jest", "karma", "react-scripts"):
            if name in deps:
                return name
        return None

    @staticmethod
    def test_command(root, path, name=None):
        """The command that runs one test file (or one test in it) with the project's Vitest, Jest or Angular's Karma."""
        runner = TypeScript.test_runner(root)
        q = _quote
        sel = (" -t " + q(name)) if name else ""
        if runner == "karma":         # Angular CLI + Karma/Jasmine runs whole files; it can't pick one test by name
            return "npx ng test --watch=false --browsers=ChromeHeadless --include %s" % q(path)
        if runner == "vitest":
            return "npx vitest run %s%s" % (q(path), sel)
        if runner == "jest":
            return "npx jest %s%s" % (q(path), sel)
        if runner == "react-scripts":
            return "npx react-scripts test --watchAll=false %s%s" % (q(path), sel)
        return None

    @staticmethod
    def prettier(root, path, text):
        """text formatted by the project's own Prettier and config, or None if the project doesn't use Prettier."""
        cli = local_bin(root, "prettier", "bin", "prettier.cjs") or local_bin(root, "prettier", "bin-prettier.js")
        node, _ = find_node()
        if not cli or not node:
            return None
        out = subprocess.run([node, cli, "--stdin-filepath", os.path.join(root, path)], input=text, capture_output=True, text=True,
                             encoding="utf-8", timeout=30, cwd=root,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if out.returncode != 0:
            raise LspError("Prettier couldn't format it: " + (out.stderr.strip().splitlines() or ["unknown error"])[0][:300])
        return out.stdout


def _quote(value):
    if re.match(r"^[\w./:@+-]+$", value):
        return value
    if sys.platform == "win32":
        return '"%s"' % value.replace('"', '\\"')
    return "'%s'" % value.replace("'", "'\\''")
