"""The Tests list of a project: every test run (yours or the AI's) and tests suggested to write or run.

A run is recorded when a command looks like a test command (pytest, npm test, go test, cargo test…)
or is started from the Tests tab. Its output is read for a summary ("3 passed, 1 failed") and the
names of failing tests. Suggestions come from the AI (its suggest_tests tool, or the ✨ Suggest
button) or from you; a suggestion with a command is marked passed or failed when that command runs.

Saved in tests.json in the app's data folder, not in the project.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time

from .store import new_id

MAX_RUNS = 200
MAX_OUTPUT = 20000

TEST_COMMAND = [re.compile(p, re.I) for p in (
    r"(^|[\s/\\])(pytest|py\.test|tox|nox|jest|vitest|mocha|ava|karma|phpunit|rspec|ctest|playwright\s+test|cypress\s+run)(\s|$)",
    r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?test",
    r"\b(go|cargo|dotnet|mvn|mvnw|mix|swift|flutter|deno|gradle|gradlew|sbt|lein|stack|cabal|zig\s+build|ant)(\.cmd|\.bat)?\"?\s+(test|verify|check)\b",
    r"\bmake\s+(test|check)\b",
    r"-m\s+(pytest|unittest|nose2?)\b",
    r"\bmanage\.py\s+test\b",
    r"\brake\s+(test|spec)\b",
    r"(^|[\s/\\])(run_?tests?|test_?\w*|\w*_?tests?)\.(py|js|mjs|ts|sh|rb|ps1|bat)\b",
)]


def is_test_command(cmd):
    c = " ".join((cmd or "").split())
    return bool(c) and any(rx.search(c) for rx in TEST_COMMAND)


_COUNT = re.compile(r"(\d+)\s+(passed|passing|failed|failing|errors?|skipped|pending|xfailed|xpassed|ignored)\b", re.I)


def parse(output, code):
    """Summary of a test run's output: counts, failing test names and a one-line summary."""
    out = output or ""
    counts = {}
    lines = out.splitlines()
    # jest/vitest: "Tests:  1 failed, 2 passed, 3 total"; pytest: "=== 2 passed, 1 failed in 0.1s ==="
    # cargo: "test result: ok. 3 passed; 0 failed"; mocha: "3 passing", "1 failing"
    summary_line = next((l for l in reversed(lines) if re.search(r"^\s*Tests?:\s", l) and _COUNT.search(l)), None) \
        or next((l for l in reversed(lines) if _COUNT.search(l)), None)
    if summary_line:
        for n, word in _COUNT.findall(summary_line):
            key = {"passing": "passed", "failing": "failed", "error": "errors", "pending": "skipped",
                   "ignored": "skipped"}.get(word.lower(), word.lower())
            counts[key] = counts.get(key, 0) + int(n)
        if "test result:" in summary_line or re.search(r"\b\d+\s+passing\b", out):
            # cargo prints one line per test binary, mocha passing/failing on separate lines: add them up
            counts = {}
            for l in lines:
                if "test result:" in l or re.match(r"^\s*\d+\s+(passing|failing|pending)\b", l):
                    for n, word in _COUNT.findall(l):
                        key = {"passing": "passed", "failing": "failed", "pending": "skipped",
                               "ignored": "skipped"}.get(word.lower(), word.lower())
                        counts[key] = counts.get(key, 0) + int(n)
    junit = re.findall(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)(?:,\s*Skipped:\s*(\d+))?", out)
    if junit:                                                                # Maven Surefire, Ant JUnit
        if re.search(r"^\[\w+\]\s+Results:", out, re.M):
            run, fail, err, skip = junit[-1]                                # Maven prints the total last
        else:
            run, fail, err, skip = (sum(int(x[i] or 0) for x in junit) for i in range(4))   # Ant: one line per class
        run, fail, err, skip = int(run), int(fail), int(err), int(skip or 0)
        counts = {"passed": run - fail - err - skip, "failed": fail}
        if err:
            counts["errors"] = err
        if skip:
            counts["skipped"] = skip
    k = re.findall(r"Executed (\d+) of (\d+)(?: \((\d+) FAILED\))?(?: \(skipped (\d+)\))?", out)       # Karma (Angular)
    if k and not junit:
        done, _total, fail, skip = (int(x or 0) for x in k[-1])
        counts = {"passed": done - fail, "failed": fail}
        if skip:
            counts["skipped"] = skip
    g = re.search(r"(\d+) tests? completed(?:, (\d+) failed)?(?:, (\d+) skipped)?", out)   # Gradle
    if g and not junit:
        done, fail, skip = int(g.group(1)), int(g.group(2) or 0), int(g.group(3) or 0)
        counts = {"passed": done - fail - skip, "failed": fail}
        if skip:
            counts["skipped"] = skip
    m = re.search(r"^Ran (\d+) tests? in", out, re.M)                     # unittest
    if m and not counts:
        total = int(m.group(1))
        f = re.search(r"^FAILED \(([^)]*)\)", out, re.M)
        bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", f.group(1))) if f else 0
        skipped = sum(int(x) for x in re.findall(r"skipped=(\d+)", out))
        counts = {"passed": total - bad - skipped, "failed": bad}
        if skipped:
            counts["skipped"] = skipped
    if not counts:                                                          # go test, or simple runners
        go_fail = len(re.findall(r"^\s*--- FAIL:", out, re.M))
        go_pass = len(re.findall(r"^\s*--- PASS:", out, re.M))
        plain_fail = len(re.findall(r"^\s*(FAIL|✗|✕)\s+\S", out, re.M))
        plain_pass = len(re.findall(r"^\s*(PASS|ok|✓|✔)\s+\S", out, re.M))
        if go_fail or go_pass:
            counts = {"passed": go_pass, "failed": go_fail}
        elif plain_fail or plain_pass:
            counts = {"passed": plain_pass, "failed": plain_fail}
    failures = []
    for rx in (r"^\[ERROR\]\s+(\w[\w.$]*\.\w+(?:\(\))?)(?::\d+)?\s", r"^\s*(?:\[junit\]\s+)?Testcase:\s+(\S+?\([^)]*\)):\s+(?:FAILED|Caused an ERROR)",
               r"^(\S+ > .+?) FAILED$", r"^FAILED\s+(\S+)", r"^\s*--- FAIL:\s+(\S+)", r"^FAIL:\s+(\S+(?: \([^)]*\))?)", r"^ERROR:\s+(\S+(?: \([^)]*\))?)",
               r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*ms\))?$", r"^\s*FAIL\s+(\S+)", r"^test (\S+) \.\.\. FAILED",
               r"^\w[\w ]*? [\d.]+ \([^)]*\)\s+(?!Executed )(.+?) FAILED$"):
        for name in re.findall(rx, out, re.M):
            name = name.strip()
            if name and not name.startswith("(") and name not in failures:
                failures.append(name)
    failures = failures[:40]
    if failures and not counts.get("failed") and not counts.get("errors"):
        counts["failed"] = len(failures)     # a runner that printed FAIL lines but exited 0 still failed
    failed = counts.get("failed", 0) + counts.get("errors", 0)
    ok = code == 0 and not failed
    parts = ["%d %s" % (counts[k], k) for k in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed") if counts.get(k)]
    if parts:
        summary = ", ".join(parts)
    elif code is None:
        summary = "stopped before it finished"
    else:
        summary = "passed" if code == 0 else "failed (exit code %s)" % code
    if code not in (0, None) and not failed and parts:
        summary += " (exit code %s)" % code
    return {"ok": ok, "summary": summary, "passed": counts.get("passed"), "failed": failed or (0 if counts else None),
            "skipped": counts.get("skipped"), "failures": failures}


def guess_command(root):
    """A likely command to run the project's tests, from the files it has."""
    j = lambda *p: os.path.join(root, *p)
    py = "python" if sys.platform == "win32" else "python3"
    try:
        with open(j("package.json"), encoding="utf-8") as f:
            scripts = (json.load(f).get("scripts") or {})
        if "test" in scripts and "no test specified" not in scripts["test"]:
            if os.path.exists(j("pnpm-lock.yaml")):
                return "pnpm test"
            if os.path.exists(j("yarn.lock")):
                return "yarn test"
            return "npm test"
    except (OSError, ValueError, AttributeError):
        pass
    if os.path.exists(j("Cargo.toml")):
        return "cargo test"
    if os.path.exists(j("go.mod")):
        return "go test ./..."
    if os.path.exists(j("pom.xml")):
        return mvn(root) + " test"
    if os.path.exists(j("gradlew")) or os.path.exists(j("gradlew.bat")):
        return ("gradlew.bat" if sys.platform == "win32" else "./gradlew") + " test"
    if os.path.exists(j("build.gradle")) or os.path.exists(j("build.gradle.kts")):
        return "gradle test"
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    if any(n.endswith((".sln", ".csproj", ".fsproj")) for n in names):
        return "dotnet test"
    if os.path.exists(j("Gemfile")) and os.path.isdir(j("spec")):
        return "bundle exec rspec"
    if os.path.exists(j("composer.json")) and (os.path.exists(j("phpunit.xml")) or os.path.exists(j("phpunit.xml.dist"))):
        return "vendor/bin/phpunit"
    for n in ("run_tests.py", "runtests.py", "run_tests.sh"):
        if n in names:
            return (py + " " + n) if n.endswith(".py") else "sh " + n
    pyish = any(n in names for n in ("pytest.ini", "conftest.py", "setup.py", "pyproject.toml", "setup.cfg", "tox.ini"))
    tests_dir = any(n in names for n in ("tests", "test"))
    test_files = any(re.match(r"(test_.*|.*_test)\.py$", n) for n in names)
    if pyish or test_files or (tests_dir and any(n.endswith(".py") for n in names)):
        return py + " -m pytest"
    if os.path.exists(j("build.xml")):                                       # Ant, e.g. a NetBeans project
        return ant() + " test"
    try:
        with open(j("Makefile"), encoding="utf-8", errors="replace") as f:
            if re.search(r"^test:", f.read(), re.M):
                return "make test"
    except OSError:
        pass
    return ""


class TestLog:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "tests.json")
        self.lock = threading.Lock()
        self.publish = lambda ev: None      # set by the server
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def _save(self):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _proj(self, pid):
        return self.data.setdefault(pid, {"runs": [], "suggested": []})

    def get(self, pid):
        with self.lock:
            d = self._proj(pid)
            return {"runs": list(d["runs"]), "suggested": list(d["suggested"])}

    def _changed(self, pid):
        self._save()
        self.publish({"type": "tests", "project": pid})

    def add_run(self, pid, command, code, output, by, seconds=None, chat=None, suggestion=None):
        r = parse(output, code)
        out = output or ""
        if len(out) > MAX_OUTPUT:
            out = out[:2000] + "\n…[%d characters cut]…\n" % (len(out) - MAX_OUTPUT) + out[-(MAX_OUTPUT - 2000):]
        run = dict(r, id=new_id(), command=command, code=code, output=out, by=by, when=time.time(),
                   seconds=round(seconds, 1) if seconds else None, chat=chat or "")
        with self.lock:
            d = self._proj(pid)
            d["runs"].insert(0, run)
            del d["runs"][MAX_RUNS:]
            norm = " ".join(command.split())
            for s in d["suggested"]:
                if (suggestion and s["id"] == suggestion) or (s.get("command") and " ".join(s["command"].split()) == norm):
                    if s["status"] != "dismissed":
                        s["status"] = "passed" if run["ok"] else "failed"
                        s["last_run"] = run["id"]
            self._changed(pid)
        return run

    def suggest(self, pid, items, by):
        added = []
        with self.lock:
            d = self._proj(pid)
            have = {s["title"].strip().lower() for s in d["suggested"]}
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                title = " ".join(str(it.get("title") or "").split())[:200]
                if not title or title.lower() in have:
                    continue
                have.add(title.lower())
                s = {"id": new_id(), "title": title, "why": str(it.get("why") or "").strip()[:1000],
                     "command": " ".join(str(it.get("command") or "").split())[:300],
                     "file": str(it.get("file") or "").strip()[:300], "status": "todo", "by": by, "when": time.time()}
                d["suggested"].append(s)
                added.append(s)
            if added:
                self._changed(pid)
        return added

    def update(self, pid, sid, fields):
        with self.lock:
            d = self._proj(pid)
            s = next((x for x in d["suggested"] if x["id"] == sid), None)
            if not s:
                raise ValueError("That suggestion is gone.")
            if fields.get("status") in ("todo", "passed", "failed", "dismissed", "done"):
                s["status"] = fields["status"]
            for k in ("title", "why", "command", "file"):
                if k in fields:
                    s[k] = " ".join(str(fields[k]).split())[:300] if k != "why" else str(fields[k])[:1000]
            self._changed(pid)
            return s

    def delete(self, pid, sid=None, runs=False):
        with self.lock:
            d = self._proj(pid)
            if runs:
                d["runs"] = []
            if sid:
                d["suggested"] = [s for s in d["suggested"] if s["id"] != sid]
                d["runs"] = [r for r in d["runs"] if r["id"] != sid]
            self._changed(pid)

    def forget(self, pid):
        with self.lock:
            if self.data.pop(pid, None) is not None:
                self._save()

    def brief(self, pid):
        """A few lines about recent test results and open suggestions, for the AI's system prompt."""
        with self.lock:
            d = self.data.get(pid) or {}
        lines = []
        runs = d.get("runs") or []
        if runs:
            r = runs[0]
            lines.append("Last test run: `%s` → %s%s" % (r["command"], r["summary"],
                         (" (failing: %s)" % ", ".join(r["failures"][:8])) if r.get("failures") else ""))
        todo = [s for s in d.get("suggested") or [] if s["status"] in ("todo", "failed")]
        if todo:
            lines.append("Suggested tests not done yet: " + "; ".join(
                s["title"] + (" [failing]" if s["status"] == "failed" else "") for s in todo[:10]))
        return "\n".join(lines)


def mvn(root):
    """The Maven command for a project: its wrapper, mvn on the PATH, or the copy that comes with NetBeans."""
    import shutil
    if os.path.exists(os.path.join(root, "mvnw.cmd" if sys.platform == "win32" else "mvnw")):
        return "mvnw.cmd" if sys.platform == "win32" else "./mvnw"
    if shutil.which("mvn"):
        return "mvn"
    return netbeans_tool(("java", "maven", "bin", "mvn.cmd" if sys.platform == "win32" else "mvn")) or "mvn"


def ant():
    """The Ant command: ant on the PATH, or the copy that comes with NetBeans."""
    import shutil
    if shutil.which("ant"):
        return "ant"
    return netbeans_tool(("extide", "ant", "bin", "ant.bat" if sys.platform == "win32" else "ant")) or "ant"


def netbeans_tool(rel):
    """A program inside a NetBeans installation (rel: path parts below the netbeans folder), quoted if needed."""
    import glob
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        bases = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        dirs = [os.path.join(b, d) for b in bases if b
                for d in ("NetBeans*", os.path.join("NetBeans*", "netbeans"), "Apache NetBeans*", os.path.join("Apache", "NetBeans*"),
                          os.path.join("Apache", "NetBeans*", "netbeans"))]
    elif sys.platform == "darwin":
        dirs = ["/Applications/*NetBeans*.app/Contents/Resources/netbeans", "/Applications/*NetBeans*.app/Contents/Resources/NetBeans/netbeans"]
    else:
        dirs = [os.path.join(home, "netbeans*"), os.path.join(home, "netbeans*", "netbeans"), "/usr/local/netbeans*", "/opt/netbeans*",
                "/opt/netbeans*/netbeans", "/usr/share/netbeans*", "/snap/netbeans/current/netbeans"]
    found = sorted({p for d in dirs for p in glob.glob(os.path.join(d, *rel))}, reverse=True)
    if not found:
        return None
    return '"%s"' % found[0] if " " in found[0] else found[0]
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        bases = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        pats = [os.path.join(b, d, "extide", "ant", "bin", "ant.bat") for b in bases if b
                for d in ("NetBeans*", os.path.join("NetBeans*", "netbeans"), "Apache NetBeans*", os.path.join("Apache", "NetBeans*"))]
    elif sys.platform == "darwin":
        pats = ["/Applications/*NetBeans*.app/Contents/Resources/netbeans/extide/ant/bin/ant",
                "/Applications/*NetBeans*.app/Contents/Resources/NetBeans/netbeans/extide/ant/bin/ant"]
    else:
        pats = [os.path.join(home, "netbeans*", "extide", "ant", "bin", "ant"), os.path.join(home, "netbeans*", "netbeans", "extide", "ant", "bin", "ant"),
                "/usr/local/netbeans*/extide/ant/bin/ant", "/opt/netbeans*/extide/ant/bin/ant", "/opt/netbeans*/netbeans/extide/ant/bin/ant",
                "/usr/share/netbeans*/extide/ant/bin/ant", "/snap/netbeans/current/netbeans/extide/ant/bin/ant"]
    found = sorted({p for pat in pats for p in glob.glob(pat)}, reverse=True)
    if found:
        return '"%s"' % found[0] if " " in found[0] else found[0]
    return "ant"
