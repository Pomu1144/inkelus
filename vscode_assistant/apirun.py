"""Running API requests the way Postman does: pre-request scripts, the request, checks, test scripts,
and variables that carry from one request to the next. Also the collection runner, its reports, and a
command-line runner for CI:

    python -m vscode_assistant.apirun collection.json [-e environment.json] [-n 3] [-d data.csv]
        [--folder "Users"] [--delay 200] [--bail] [--junit report.xml] [--html report.html]

Scripts are JavaScript (Postman's pm.* API). In the app they run in a sandboxed worker in the app
window; on the command line they run with Node.js (pmsandbox.js), so Node is needed there only if the
collection has scripts.
"""
import copy
import csv
import html
import io
import json
import os
import shutil
import subprocess
import sys
import time
from xml.sax.saxutils import escape as xml_escape

from . import api

SANDBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pmsandbox.js")
MAX_STEPS = 2000


def node_runner(ctx):
    """Run one script with Node.js (used when there's no app window, e.g. on the command line)."""
    node = shutil.which("node")
    if not node:
        return {"error": "This collection has scripts, which need Node.js on the command line (or the app window).",
                "scopes": ctx["scopes"], "request": ctx["request"], "tests": [], "console": []}
    try:
        p = subprocess.run([node, SANDBOX], input=json.dumps(ctx).encode(), capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        return json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        return {"error": "The script didn't finish: %s" % e, "scopes": ctx["scopes"], "request": ctx["request"],
                "tests": [], "console": []}


def response_for_script(res):
    return {"status": res.get("status"), "reason": res.get("reason", ""), "ms": res.get("ms"), "size": res.get("size"),
            "headers": [list(h) for h in res.get("headers") or []], "body": res.get("body") or "",
            "cookies": res.get("cookies") or []}


class Runner:
    """Executes requests for one project ("" = global) against an api.Store."""

    def __init__(self, store, project="", script_runner=None, persist=True, defaults=None):
        self.store = store
        self.project = project or ""
        self.script_runner = script_runner or node_runner
        self.persist = persist
        self.defaults = defaults or {}      # e.g. baseUrl from the project's dev server: below every other scope

    # variable scopes, lowest priority first: globals < collection < environment < data < local
    def scopes(self, col, env, data=None, local=None):
        return {"globals": dict(self.defaults, **api.kv_dict(self.store.data["globals"].get(self.project, []))),
                "collection": api.kv_dict((col or {}).get("variables")),
                "environment": api.kv_dict((env or {}).get("vars")),
                "data": dict(data or {}), "local": local if local is not None else {}}

    @staticmethod
    def merged(sc):
        out = {}
        for k in ("globals", "collection", "environment", "data", "local"):
            out.update(sc[k])
        return out

    def execute(self, col, req, chain, env=None, data=None, local=None, iteration=0, iterations=1):
        """One request with its scripts. chain = [collection, folders…, request] for inheritance."""
        col = col or api.blank_collection("")
        req = copy.deepcopy(req)
        chain = list(chain[:-1]) + [req] if chain else [col, req]
        sc = self.scopes(col, env, data, local)
        before = copy.deepcopy(sc)
        result = {"name": req.get("name", ""), "id": req.get("id", ""), "tests": [], "console": [], "script_errors": [],
                  "next_request": "__next__", "skipped": False}
        info = {"requestName": req.get("name", ""), "requestId": req.get("id", ""), "iteration": iteration,
                "iterationCount": iterations}

        def script(node, kind, response=None):
            code = (node.get(kind) or "").strip()
            if not code:
                return
            where = "collection" if node is col else "folder %r" % node.get("name") if node.get("folder") else "request"
            out = self.script_runner({"script": code, "scopes": sc, "request": req, "response": response,
                                      "info": dict(info, eventName="test" if response else "prerequest"),
                                      "noEnvironment": env is None}) or {}
            for k in sc:
                if isinstance((out.get("scopes") or {}).get(k), dict):
                    sc[k] = out["scopes"][k]
            if isinstance(out.get("request"), dict) and not response:
                req.update(out["request"])
            for t in out.get("tests") or []:
                result["tests"].append(dict(t, where=where))
            for c in out.get("console") or []:
                result["console"].append(dict(c, where=where))
            if out.get("error"):
                result["script_errors"].append("%s %s script: %s" % (where.capitalize(), "test" if response else "pre-request", out["error"]))
            if "nextRequest" in out:          # a name jumps there; null (None) ends the run
                result["next_request"] = out["nextRequest"]
            if out.get("skip"):
                result["skipped"] = True

        for node in chain:
            script(node, "pre_script")
            if result["skipped"]:
                break
        vals = self.merged(sc)
        secrets = api.variables(env)[1]
        if result["skipped"]:
            res = {"method": req.get("method"), "url": api.fill(req.get("url"), vals), "skipped": True}
        else:
            settings = dict(self.store.settings(), **{k: v for k, v in (req.get("settings") or {}).items() if v not in (None, "")})
            res = api.send(req, vals, auth=api.effective_auth(chain), store=self.store, project=self.project, settings=settings)
        result["response"] = res
        result["checks"] = api.run_checks(req.get("checks") or [], res) if not result["skipped"] else []
        if not result["skipped"] and not res.get("error"):
            rs = response_for_script(res)
            for node in chain:
                script(node, "test_script", rs)
        result["missing"] = res.get("missing") or []
        failed = bool(res.get("error")) or any(not c["pass"] for c in result["checks"]) or \
            any(not t["passed"] for t in result["tests"]) or bool(result["script_errors"])
        result["pass"] = not failed and not result["skipped"]
        result["secrets"] = sorted(secrets)
        if self.persist:
            self.save_scopes(col, env, before, sc)
        result["local"] = sc["local"]
        return result

    def save_scopes(self, col, env, before, after):
        """Write variables that scripts set or unset back to the environment, collection and globals."""
        changed = False

        def apply(rows, old, new, secret_ok=False):
            nonlocal changed
            if old == new:
                return rows
            changed = True
            rows = [r for r in rows if not (r.get("key") in old and r.get("key") not in new)]
            for k, v in new.items():
                row = next((r for r in rows if r.get("key") == k), None)
                if row is None:
                    rows.append({"key": k, "value": v, "enabled": True, **({"secret": False} if secret_ok else {})})
                elif str(row.get("value")) != v:
                    row["value"] = v
            return rows
        with self.store.lock:
            if env is not None:
                live = next((e for e in self.store.data["environments"] if e["id"] == env.get("id")), None)
                if live is not None:
                    live["vars"] = apply(live.get("vars") or [], before["environment"], after["environment"], True)
                    env["vars"] = live["vars"]
            if col is not None and col.get("id"):
                live = self.store.collection(col["id"])
                if live is not None and before["collection"] != after["collection"]:
                    live["variables"] = apply(live.get("variables") or [], before["collection"], after["collection"])
                    col["variables"] = live["variables"]
                    if live.get("location") == "project":
                        self.store.persist(live)
            g = self.store.data["globals"].get(self.project, [])
            self.store.data["globals"][self.project] = apply(g, before["globals"], after["globals"])
            if changed:
                self.store.save()

    def run(self, col, env=None, folder=None, iterations=1, data=None, delay_ms=0, stop_on_fail=False,
            on_result=None, cancel=None):
        """Run every request (in a folder, or the whole collection) once per iteration / data row."""
        root = folder or col
        reqs = api.requests_in(root)
        chains = {r["id"]: api.find(col, r["id"])[1] for r in reqs}
        rows = data or []
        n = max(int(iterations or 1), len(rows)) if rows else max(int(iterations or 1), 1)
        results, t0, stopped = [], time.time(), False
        local = {}
        for it in range(n):
            row = rows[it % len(rows)] if rows else {}
            i, steps = 0, 0
            while 0 <= i < len(reqs):
                if cancel is not None and cancel.is_set():
                    stopped = True
                    break
                steps += 1
                if steps > MAX_STEPS:
                    break
                r = reqs[i]
                res = self.execute(col, r, chains[r["id"]], env, row, local, it, n)
                res["iteration"] = it
                results.append(res)
                if on_result:
                    on_result(res)
                if stop_on_fail and not res["pass"] and not res["skipped"]:
                    stopped = True
                    break
                nxt = res["next_request"]
                if nxt is None:
                    break
                if nxt != "__next__":
                    j = next((k for k, x in enumerate(reqs) if x.get("name") == nxt), None)
                    if j is None:
                        res["script_errors"].append("setNextRequest: no request called %r in this run." % nxt)
                        i += 1
                    else:
                        i = j
                else:
                    i += 1
                if delay_ms and i < len(reqs):
                    time.sleep(min(float(delay_ms), 60000) / 1000)
            if stopped:
                break
        return summarize(root.get("name", ""), results, time.time() - t0, n, stopped)


def summarize(name, results, seconds, iterations, stopped=False):
    tests = sum(len(r["tests"]) + len(r["checks"]) for r in results)
    failed_tests = sum(sum(1 for t in r["tests"] if not t["passed"]) + sum(1 for c in r["checks"] if not c["pass"]) for r in results)
    return {"name": name, "results": results, "requests": len(results), "passed": sum(1 for r in results if r["pass"]),
            "failed": sum(1 for r in results if not r["pass"] and not r["skipped"]), "skipped": sum(1 for r in results if r["skipped"]),
            "tests": tests, "failed_tests": failed_tests, "seconds": round(seconds, 2), "iterations": iterations, "stopped": stopped}


def text_report(rep, secrets=()):
    """PASS/FAIL lines (the Tests list reads these) and a summary."""
    lines = []
    for r in rep["results"]:
        res = r["response"]
        tag = "SKIP" if r["skipped"] else "PASS" if r["pass"] else "FAIL"
        what = res.get("error") or "%s, %s ms" % (res.get("status"), res.get("ms"))
        lines.append("%s %s%s (%s)" % (tag, r["name"], " [#%d]" % (r["iteration"] + 1) if rep["iterations"] > 1 else "", what))
        for c in r["checks"]:
            if not c["pass"]:
                lines.append("    %s: got %s" % (c["label"], c["actual"]))
        for t in r["tests"]:
            if not t["passed"]:
                lines.append("    %s: %s" % (t["name"], t.get("error") or "failed"))
        for e in r["script_errors"]:
            lines.append("    " + e)
    lines.append("%d passed, %d failed" % (rep["passed"], rep["failed"]) + (" (stopped early)" if rep["stopped"] else ""))
    return api.mask("\n".join(lines), set(secrets))


def junit_report(rep):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<testsuites name="%s" tests="%d" failures="%d" time="%.3f">' % (xml_escape(rep["name"]), rep["tests"] or rep["requests"],
                                                                         rep["failed_tests"] or rep["failed"], rep["seconds"])]
    for r in rep["results"]:
        res = r["response"]
        cases = [(c["label"], c["pass"], "got %s" % c["actual"]) for c in r["checks"]] + \
                [(t["name"], t["passed"], t.get("error", "")) for t in r["tests"]]
        if res.get("error"):
            cases.append(("Request sent", False, res["error"]))
        if not cases:
            cases = [("Request sent", True, "")]
        fails = sum(1 for c in cases if not c[1])
        out.append('  <testsuite name="%s" tests="%d" failures="%d" time="%.3f">' % (
            xml_escape(r["name"] + (" #%d" % (r["iteration"] + 1) if rep["iterations"] > 1 else "")), len(cases), fails, (res.get("ms") or 0) / 1000))
        for name, ok, msg in cases:
            out.append('    <testcase name="%s" classname="%s">' % (xml_escape(name), xml_escape(r["name"])) +
                       ("" if ok else '<failure message="%s"/>' % xml_escape(msg, {'"': "&quot;"})) + "</testcase>")
        out.append("  </testsuite>")
    out.append("</testsuites>")
    return "\n".join(out)


def html_report(rep):
    rows = []
    for r in rep["results"]:
        res = r["response"]
        status = "skipped" if r["skipped"] else "pass" if r["pass"] else "fail"
        details = "".join("<li class='%s'>%s%s</li>" % ("pass" if c["pass"] else "fail", html.escape(c["label"]),
                                                       "" if c["pass"] else " — got " + html.escape(str(c["actual"]))) for c in r["checks"])
        details += "".join("<li class='%s'>%s%s</li>" % ("pass" if t["passed"] else "fail", html.escape(t["name"]),
                                                        "" if t["passed"] else " — " + html.escape(t.get("error", ""))) for t in r["tests"])
        details += "".join("<li class='fail'>%s</li>" % html.escape(e) for e in r["script_errors"])
        rows.append("<tr class='%s'><td>%s</td><td><b>%s</b> %s</td><td>%s</td><td>%s</td><td><ul>%s</ul></td></tr>" % (
            status, r["iteration"] + 1, html.escape(res.get("method") or ""), html.escape(r["name"]),
            html.escape(str(res.get("error") or res.get("status"))), res.get("ms") or "", details))
    return """<!doctype html><meta charset="utf-8"><title>%s · API run</title>
<style>body{font:14px system-ui,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%%}
td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}tr.fail td:first-child{border-left:4px solid #c33}
tr.pass td:first-child{border-left:4px solid #2a7}li.fail{color:#c33}li.pass{color:#2a7}ul{margin:0;padding-left:18px}</style>
<h1>%s</h1><p>%d requests · <b>%d passed</b> · <b>%d failed</b> · %d checks and tests, %d failed · %d iteration(s) · %.1fs</p>
<table><tr><th>#</th><th>Request</th><th>Status</th><th>ms</th><th>Checks and tests</th></tr>%s</table>""" % (
        html.escape(rep["name"]), html.escape(rep["name"]), rep["requests"], rep["passed"], rep["failed"], rep["tests"],
        rep["failed_tests"], rep["iterations"], rep["seconds"], "".join(rows))


def json_report(rep):
    slim = dict(rep, results=[{k: v for k, v in dict(r, response={k2: v2 for k2, v2 in r["response"].items()
                                                                 if k2 not in ("body_b64", "json")}).items()} for r in rep["results"]])
    return json.dumps(slim, indent=2, ensure_ascii=False, default=str)


def parse_data(text, filename=""):
    """Iteration data: a CSV file (first row = variable names) or a JSON array of objects."""
    t = (text or "").lstrip("﻿").strip()
    if not t:
        return []
    if filename.lower().endswith(".json") or t[:1] in ("[", "{"):
        d = json.loads(t)
        if isinstance(d, dict):
            d = [d]
        if not isinstance(d, list) or not all(isinstance(x, dict) for x in d):
            raise ValueError("A JSON data file must be a list of objects, e.g. [{\"name\": \"Ada\"}].")
        return [{k: v if isinstance(v, str) else json.dumps(v) for k, v in x.items()} for x in d]
    return [dict(r) for r in csv.DictReader(io.StringIO(t))]


# ── command line ─────────────────────────────────────────────────────────
def main(argv=None):
    import argparse
    import tempfile
    ap = argparse.ArgumentParser(description="Run an API collection (Postman v2.1 format or this app's) and its checks and tests.")
    ap.add_argument("collection", help="collection .json file (exported from this app or Postman)")
    ap.add_argument("-e", "--environment", help="environment .json (Postman export, or {\"name\": \"value\"})")
    ap.add_argument("-n", "--iterations", type=int, default=1)
    ap.add_argument("-d", "--data", help="iteration data: CSV or JSON")
    ap.add_argument("--folder", help="run only this folder")
    ap.add_argument("--delay", type=int, default=0, help="milliseconds between requests")
    ap.add_argument("--bail", action="store_true", help="stop at the first failing request")
    ap.add_argument("--timeout", type=int, default=30000, help="request timeout in ms")
    ap.add_argument("--insecure", action="store_true", help="don't check TLS certificates")
    ap.add_argument("--junit", help="write a JUnit XML report here")
    ap.add_argument("--html", help="write an HTML report here")
    ap.add_argument("--json", help="write a JSON report here")
    a = ap.parse_args(argv)
    with open(a.collection, encoding="utf-8") as f:
        doc = json.load(f)
    col = api.from_postman(doc)[0] if "item" in doc else api.migrate(doc)
    env = None
    if a.environment:
        with open(a.environment, encoding="utf-8") as f:
            e = json.load(f)
        vals = e.get("values") if isinstance(e.get("values"), list) else [{"key": k, "value": v} for k, v in e.items()]
        env = {"id": api.new_id(), "name": e.get("name", "environment"), "vars": [{"key": v.get("key"), "value": str(v.get("value", "")),
                                                                         "enabled": v.get("enabled", True)} for v in vals]}
    data = []
    if a.data:
        with open(a.data, encoding="utf-8") as f:
            data = parse_data(f.read(), a.data)
    tmp = tempfile.mkdtemp(prefix="api-run-")
    store = api.Store(tmp)        # a scratch store, so values scripts set carry on to the next requests
    store.data["settings"] = {"timeout_ms": a.timeout, "verify": not a.insecure}
    store.data["collections"].append(col)
    if env:
        store.data["environments"].append(env)
    folder = None
    if a.folder:
        folder = next((n for n, _ in api.walk(col["items"]) if n.get("folder") and n.get("name") == a.folder), None)
        if folder is None:
            raise SystemExit("No folder called %r in the collection." % a.folder)
    runner = Runner(store, "", node_runner)

    def show(r):
        res = r["response"]
        mark = "SKIP" if r["skipped"] else "PASS" if r["pass"] else "FAIL"
        print("%s %s %s  %s" % (mark, res.get("method") or "", r["name"], res.get("error") or "%s · %s ms" % (res.get("status"), res.get("ms"))))
        for c in r["checks"]:
            if not c["pass"]:
                print("    FAIL %s (got %s)" % (c["label"], c["actual"]))
        for t in r["tests"]:
            print("    %s %s%s" % ("pass" if t["passed"] else "FAIL", t["name"], "" if t["passed"] else ": " + t.get("error", "")))
        for e in r["script_errors"]:
            print("    ! " + e)
    try:
        rep = runner.run(col, env, folder, a.iterations, data, a.delay, a.bail, on_result=show)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d requests: %d passed, %d failed · %d checks/tests, %d failed · %.1fs" % (
        rep["requests"], rep["passed"], rep["failed"], rep["tests"], rep["failed_tests"], rep["seconds"]))
    for path, fn in ((a.junit, junit_report), (a.html, html_report), (a.json, json_report)):
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(fn(rep))
    return 1 if rep["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
