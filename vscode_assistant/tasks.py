"""What kind of project a folder is, and the commands to build, run, test and clean it.

The Projects page shows these as buttons (they run in its Terminal), and the AI is told them, so
"how do I run this?" has a real answer. NetBeans projects are covered: Ant projects (nbproject/ and
build.xml) use the Ant that comes with NetBeans when ant isn't installed separately; NetBeans Maven
and Gradle projects are plain Maven / Gradle projects.
"""
import json
import os
import re
import sys

from . import testlog


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def detect(root):
    """{"type": label, "tasks": [{"name", "command"}]} for the project at root (tasks may be empty)."""
    j = lambda *p: os.path.join(root, *p)
    tasks = []

    def add(name, cmd):
        if cmd and not any(t["name"] == name for t in tasks):
            tasks.append({"name": name, "command": cmd})

    if os.path.exists(j("pom.xml")):
        m = testlog.mvn(root)
        pom = _read(j("pom.xml"))
        add("Build", m + " compile")
        if "spring-boot-maven-plugin" in pom:
            add("Run", m + " spring-boot:run")
        elif "exec-maven-plugin" in pom or "<mainClass>" in pom:
            add("Run", m + " exec:java")
        elif "<packaging>war</packaging>" in pom:
            for goal, plug in (("jetty:run", "jetty-maven-plugin"), ("tomcat7:run", "tomcat7-maven-plugin"),
                               ("liberty:dev", "liberty-maven-plugin"), ("wildfly:run", "wildfly-maven-plugin")):
                if plug in pom:
                    add("Run", m + " " + goal)
                    break
        add("Test", m + " test")
        add("Package", m + " package")
        add("Clean", m + " clean")
        kind = "Java (Maven)" + (" · NetBeans" if os.path.exists(j("nb-configuration.xml")) or os.path.exists(j("nbactions.xml")) else "")
        return {"type": kind, "tasks": tasks}
    if any(os.path.exists(j(f)) for f in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        g = ("gradlew.bat" if sys.platform == "win32" else "./gradlew") if os.path.exists(j("gradlew")) or os.path.exists(j("gradlew.bat")) else "gradle"
        build = _read(j("build.gradle")) + _read(j("build.gradle.kts"))
        add("Build", g + " build -x test")
        if "org.springframework.boot" in build:
            add("Run", g + " bootRun")
        elif re.search(r"\bapplication\b", build):
            add("Run", g + " run")
        add("Test", g + " test")
        add("Clean", g + " clean")
        android = "com.android" in build
        return {"type": "Android (Gradle)" if android else "Java/Kotlin (Gradle)", "tasks": tasks}
    if os.path.exists(j("build.xml")):
        a = testlog.ant()
        names = set(re.findall(r"<target\s+[^>]*name=\"([^\"]+)\"", _read(j("build.xml"))))
        netbeans = os.path.isdir(j("nbproject"))
        if netbeans:      # NetBeans' generated build-impl.xml has these targets
            names |= set(re.findall(r"<target\s+[^>]*name=\"([^\"]+)\"", _read(j("nbproject", "build-impl.xml"))))
        pick = lambda *opts: next((o for o in opts if o in names), None)
        add("Build", a + " " + (pick("jar", "compile", "build", "dist", "default") or ""))
        add("Run", a + " " + pick("run", "run-main") if pick("run", "run-main") else "")
        add("Test", a + " " + pick("test", "junit", "tests") if pick("test", "junit", "tests") else "")
        add("Clean", a + " clean" if "clean" in names else "")
        if not tasks:
            add("Build", a)
        return {"type": "Java (NetBeans Ant)" if netbeans else "Java (Ant)", "tasks": tasks}
    try:
        pkg = json.loads(_read(j("package.json")) or "null")
    except ValueError:
        pkg = None
    if isinstance(pkg, dict):
        pm = "pnpm" if os.path.exists(j("pnpm-lock.yaml")) else "yarn" if os.path.exists(j("yarn.lock")) else "npm"
        scripts = pkg.get("scripts") or {}
        run = lambda s: ("%s %s" % (pm, s)) if pm != "npm" or s in ("start", "test") else "npm run " + s
        for name, keys in (("Run", ("dev", "start", "serve")), ("Build", ("build",)), ("Test", ("test",)), ("Lint", ("lint",))):
            k = next((x for x in keys if x in scripts and "no test specified" not in scripts[x]), None)
            if k:
                add(name, run(k))
        add("Install", pm + " install")
        return {"type": "TypeScript/JavaScript" if os.path.exists(j("tsconfig.json")) else "JavaScript (Node)", "tasks": tasks}
    if os.path.exists(j("Cargo.toml")):
        for n in ("build", "run", "test", "clean"):
            add(n.capitalize(), "cargo " + n)
        return {"type": "Rust (Cargo)", "tasks": tasks}
    if os.path.exists(j("go.mod")):
        add("Build", "go build ./...")
        add("Run", "go run .")
        add("Test", "go test ./...")
        return {"type": "Go", "tasks": tasks}
    names = []
    try:
        names = os.listdir(root)
    except OSError:
        pass
    if any(n.endswith((".sln", ".csproj", ".fsproj")) for n in names):
        for n in ("build", "run", "test", "clean"):
            add(n.capitalize(), "dotnet " + n)
        return {"type": ".NET", "tasks": tasks}
    py = "python" if sys.platform == "win32" else "python3"
    if any(n.endswith(".py") for n in names) or os.path.exists(j("pyproject.toml")):
        main = next((n for n in ("main.py", "app.py", "manage.py", "run.py", "server.py") if n in names), None)
        if main == "manage.py":
            add("Run", py + " manage.py runserver")
        elif main:
            add("Run", "%s %s" % (py, main))
        add("Test", testlog.guess_command(root))
        if os.path.exists(j("requirements.txt")):
            add("Install", py + " -m pip install -r requirements.txt")
        return {"type": "Python", "tasks": tasks}
    mk = _read(j("Makefile"))
    if mk:
        for t in re.findall(r"^([A-Za-z][\w-]*):(?!=)", mk, re.M)[:8]:
            add(t.capitalize(), "make " + t)
        return {"type": "Make", "tasks": tasks}
    return {"type": "", "tasks": tasks}
