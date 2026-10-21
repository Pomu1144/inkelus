// The API tab: build and send requests, check responses, keep collections, run the dev server,
// and compare the endpoints in the code with the docs. app.js calls VAApi(app) once.
window.VAApi = function (app) {
  "use strict";
  const { api, toast, guard, esc, openModal, closeModal, $, $$ } = app;
  const A = {
    project: "", scope: null, server: null, tasks: null, req: null, colId: "", dirty: false, res: null,
    reqTab: "params", resTab: "body", sideTab: "collections", routes: null, open: new Set(), raw: false, log: "",
  };
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { /* private window */ } return null; };
  const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const blank = (name, method, url) => ({ id: uid(), name: name || "New request", method: method || "GET", url: url || "{{baseUrl}}/",
    params: [], headers: [], body: { mode: "none", text: "", form: [] }, auth: { type: "none" },
    checks: [{ kind: "status", op: "eq", value: "2xx", enabled: true }] });
  const clone = o => JSON.parse(JSON.stringify(o));
  const q = () => "project=" + encodeURIComponent(A.project);

  // ── loading ──────────────────────────────────────────────────────────
  async function show() {
    const r = await api("GET", "projects");
    const sel = $("#apiProject");
    const prev = A.project || store("va-api-project") || "";
    sel.innerHTML = '<option value="">No project (global)</option>' + r.projects.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + "</option>").join("");
    A.project = r.projects.some(p => p.id === prev) ? prev : "";
    sel.value = A.project;
    await load();
    if (!A.req) newRequest();
    render();
  }
  async function load() {
    const s = await api("GET", "http/state?" + q());
    A.scope = s;
    A.server = s.server || null;
    A.tasks = s.tasks || null;
    A.log = (s.server && s.server.log) || "";
  }

  // ── rendering ────────────────────────────────────────────────────────
  function render() {
    renderEnv();
    renderServer();
    renderSide();
    renderReq();
    renderRes();
  }
  function renderEnv() {
    const envs = A.scope.environments;
    $("#apiEnv").innerHTML = '<option value="">No environment</option>' + envs.map(e => '<option value="' + esc(e.id) + '">' + esc(e.name) +
      (e.project ? "" : " (global)") + "</option>").join("");
    $("#apiEnv").value = envs.some(e => e.id === A.scope.active_env) ? A.scope.active_env : "";
  }
  function renderServer() {
    const bar = $("#apiServer");
    bar.hidden = !A.project;
    if (!A.project || !A.server) return;
    const s = A.server, cfg = s.config || {};
    const st = s.state || "stopped";
    const label = { running: "Running", starting: "Starting…", exited: "Stopped (exit code " + (s.code ?? "?") + ")", stopped: "Stopped" }[st] || st;
    bar.innerHTML = '<span class="srv-dot ' + esc(st) + '"></span><b>Dev server</b><span class="faint small">' + esc(label) + "</span>" +
      '<input id="srvCmd" spellcheck="false" placeholder="Command, e.g. npm run dev" value="' + esc(cfg.command || "") + '">' +
      '<input id="srvUrl" spellcheck="false" placeholder="http://localhost:3000" value="' + esc(cfg.url || "") + '">' +
      (st === "running" || st === "starting" ? '<button class="btn small" data-srv="restart">' + icon("refresh") + "Restart</button>" +
        '<button class="btn small" data-srv="stop">' + icon("stop") + "Stop</button>"
        : '<button class="btn small primary" data-srv="start">' + icon("play") + "Start</button>") +
      '<button class="btn small" data-srv="log">' + icon(A.showLog ? "chevron-up" : "chevron-down") + "Log</button>" +
      (A.showLog ? '<pre class="srv-log" id="srvLog">' + esc(A.log.slice(-40000) || "(no output yet)") + "</pre>" : "");
    if (A.showLog) { const l = $("#srvLog"); l.scrollTop = l.scrollHeight; }
  }
  function renderSide() {
    $$(".api-side .tab").forEach(t => t.classList.toggle("active", t.dataset.aside === A.sideTab));
    $("#apiEndpTab").hidden = !A.project;
    if (A.sideTab === "endpoints" && !A.project) A.sideTab = "collections";
    const box = $("#apiSide");
    if (A.sideTab === "collections") {
      const cols = A.scope.collections;
      box.innerHTML = '<div class="t-sec"><span>Collections</span><span><button class="icon-btn" data-c="newcol" title="New collection">' + icon("folder-plus") +
        "</button></span></div>" + (cols.length ? cols.map(c => {
          const open = A.open.has(c.id);
          return '<div class="col' + (open ? " open" : "") + '" data-col="' + esc(c.id) + '"><div class="col-head" data-c="toggle">' + icon(open ? "chevron-down" : "chevron-right", "tw") +
            '<span class="cn">' + esc(c.name) + '</span><span class="faint tiny">' + c.requests.length + (c.project ? "" : " · global") + "</span>" +
            '<span class="col-acts"><button class="icon-btn" data-c="run" title="Run every request and its checks">' + icon("play") + "</button>" +
            '<button class="icon-btn" data-c="add" title="New request in this collection">' + icon("plus") + "</button>" +
            '<button class="icon-btn" data-c="more" title="Rename, export or delete">' + icon("edit") + "</button></span></div>" +
            (open ? c.requests.map(r => '<div class="rq' + (A.req && A.req.id === r.id ? " active" : "") + '" data-rq="' + esc(r.id) + '"><b class="m m-' + esc(r.method) + '">' +
              esc(r.method === "DELETE" ? "DEL" : r.method === "OPTIONS" ? "OPT" : r.method) + "</b><span>" + esc(r.name) + "</span></div>").join("") +
              (c.requests.length ? "" : '<p class="faint tiny pad">Empty. Save a request here, or press +.</p>') : "") + "</div>";
        }).join("") : '<p class="faint small pad">No collections yet. Build a request and press Save, or Import a Postman collection or curl command.</p>');
    } else if (A.sideTab === "history") {
      const h = A.scope.history;
      box.innerHTML = '<div class="t-sec"><span>History</span>' + (h.length ? '<button class="icon-btn" data-c="clearhist" title="Clear the history">' + icon("clear") + "</button>" : "") + "</div>" +
        (h.length ? h.map(x => '<div class="hi" data-hist="' + esc(x.id) + '"><b class="m m-' + esc(x.request.method) + '">' + esc(x.request.method.slice(0, 4)) + '</b><span class="hu">' +
          esc(x.request.url) + '</span><span class="hs ' + (x.error ? "bad" : x.status >= 400 ? "bad" : "ok") + '">' + esc(x.error ? "error" : String(x.status)) + "</span>" +
          (x.by === "ai" ? '<span class="tag">AI</span>' : "") + "</div>").join("") : '<p class="faint small pad">Requests you and the AI send show up here.</p>');
    } else {
      const r = A.routes;
      box.innerHTML = '<div class="t-sec"><span>Endpoints in the code</span><span><button class="btn small" data-c="scan">' + icon("refresh") + "Scan</button></span></div>" +
        (r ? (r.length ? '<div class="row tight"><button class="btn small" data-c="addall">Add all to a collection</button><button class="btn small" data-c="compare">Compare with docs</button></div>' +
          r.map((x, i) => '<div class="ep" data-ep="' + i + '" title="' + esc(x.file + (x.line ? ":" + x.line : "")) + '"><b class="m m-' + esc(x.method) + '">' + esc(x.method.slice(0, 4)) +
            '</b><span class="hu">' + esc(x.path) + '</span><span class="faint tiny">' + esc(x.file.split("/").pop()) + "</span></div>").join("")
          : '<p class="faint small pad">No endpoints found. Supported: Express, FastAPI, Flask, Django, Spring, JAX-RS, ASP.NET, Go, Rails, Laravel and OpenAPI JSON files.</p>')
          : '<p class="faint small pad">Find the HTTP endpoints your code defines, try them, and check the docs list the same ones.</p>');
    }
  }
  function kvRows(list, kind) {
    const rows = list.concat([{ key: "", value: "", enabled: true, _new: true }]);
    return '<div class="kv" data-kv="' + kind + '">' + rows.map((r, i) => '<div class="kvr' + (r._new ? " new" : "") + '" data-i="' + i + '">' +
      '<input type="checkbox" data-f="enabled"' + (r.enabled !== false ? " checked" : "") + (r._new ? " hidden" : "") + ">" +
      '<input data-f="key" placeholder="' + (kind === "headers" ? "Header" : "Name") + '" value="' + esc(r.key || "") + '" spellcheck="false">' +
      '<input data-f="value" placeholder="Value" value="' + esc(r.value || "") + '" spellcheck="false">' +
      (r._new ? "<span></span>" : '<button class="icon-btn" data-kvdel="' + i + '" title="Remove">' + icon("close") + "</button>") + "</div>").join("") + "</div>";
  }
  const CHECK_OPS = {
    status: [["eq", "is"], ["ne", "is not"]], time: [["lt", "under (ms)"]], body: [["contains", "contains"], ["not_contains", "doesn't contain"]],
    header: [["exists", "exists"], ["eq", "is"], ["contains", "contains"]],
    json: [["exists", "exists"], ["missing", "is absent"], ["eq", "equals"], ["ne", "isn't"], ["contains", "contains"], ["type", "is a (type)"],
      ["length", "has length"], ["gt", ">"], ["lt", "<"]],
  };
  function renderReq() {
    const r = A.req;
    $("#reqMethod").value = r.method;
    $("#reqMethod").className = "m-" + r.method;
    if (document.activeElement !== $("#reqUrl")) $("#reqUrl").value = r.url;
    if (document.activeElement !== $("#reqName")) $("#reqName").value = r.name;
    const col = A.scope.collections.find(c => c.id === A.colId);
    $("#reqWhere").textContent = col ? "in " + col.name + (A.dirty ? " · unsaved changes" : "") : "not saved yet";
    $$(".req-tabs .tab").forEach(t => {
      t.classList.toggle("active", t.dataset.rtab === A.reqTab);
      const n = { params: r.params.length, headers: r.headers.length, checks: r.checks.length }[t.dataset.rtab];
      t.querySelector("b").textContent = n ? n : t.dataset.rtab === "body" && r.body.mode !== "none" ? r.body.mode : t.dataset.rtab === "auth" && r.auth.type !== "none" ? r.auth.type : "";
    });
    const pane = $("#reqPane");
    if (A.reqTab === "params") pane.innerHTML = kvRows(r.params, "params") + '<p class="faint tiny">Added to the URL as ?name=value. Use {{variables}} anywhere.</p>';
    else if (A.reqTab === "headers") pane.innerHTML = kvRows(r.headers, "headers");
    else if (A.reqTab === "body") {
      const b = r.body;
      pane.innerHTML = '<div class="row tight">' + [["none", "None"], ["json", "JSON"], ["text", "Text"], ["form", "Form"]].map(([v, l]) =>
        '<label class="check"><input type="radio" name="bmode" value="' + v + '"' + (b.mode === v ? " checked" : "") + "> " + l + "</label>").join("") +
        (b.mode === "json" ? '<button class="btn small" data-b="format">Format</button>' : "") + "</div>" +
        (b.mode === "json" || b.mode === "text" ? '<textarea id="bodyText" spellcheck="false" placeholder=\'' + (b.mode === "json" ? '{ "name": "Ada" }' : "") + "'>" + esc(b.text || "") + "</textarea>" : "") +
        (b.mode === "form" ? kvRows(b.form, "form") : "") + (b.mode === "none" ? '<p class="faint small">This request has no body.</p>' : "");
    } else if (A.reqTab === "auth") {
      const a = r.auth;
      const f = (k, l, ph, type) => '<label>' + l + '<input data-auth="' + k + '" type="' + (type || "text") + '" value="' + esc(a[k] || "") + '" placeholder="' + esc(ph || "") + '" spellcheck="false"></label>';
      pane.innerHTML = '<label>Type<select data-auth="type">' + [["none", "None"], ["bearer", "Bearer token"], ["basic", "Basic (username and password)"], ["apikey", "API key"]].map(([v, l]) =>
        '<option value="' + v + '"' + (a.type === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select></label>" +
        (a.type === "bearer" ? f("token", "Token", "{{token}}") : "") +
        (a.type === "basic" ? '<div class="grid2">' + f("username", "Username") + f("password", "Password", "{{password}}", "password") + "</div>" : "") +
        (a.type === "apikey" ? '<div class="grid2">' + f("key", "Name", "X-API-Key") + f("value", "Value", "{{apiKey}}") + '</div><label>Send it in<select data-auth="in"><option value="header"' +
          (a.in !== "query" ? " selected" : "") + '>a header</option><option value="query"' + (a.in === "query" ? " selected" : "") + ">the URL (query)</option></select></label>" : "") +
        '<p class="faint tiny">Tip: keep tokens in an environment variable marked secret, e.g. {{token}}. The AI never sees secret values.</p>';
    } else {
      pane.innerHTML = '<div class="checks">' + r.checks.map((c, i) => '<div class="ck" data-ck="' + i + '"><input type="checkbox" data-k="enabled"' + (c.enabled !== false ? " checked" : "") + ">" +
        '<select data-k="kind">' + [["status", "Status"], ["json", "JSON field"], ["header", "Header"], ["body", "Body text"], ["time", "Time"]].map(([v, l]) =>
          '<option value="' + v + '"' + (c.kind === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select>" +
        (c.kind === "json" || c.kind === "header" ? '<input data-k="path" placeholder="' + (c.kind === "json" ? "data[0].id" : "Content-Type") + '" value="' + esc(c.path || "") + '" spellcheck="false">' : "") +
        '<select data-k="op">' + (CHECK_OPS[c.kind] || []).map(([v, l]) => '<option value="' + v + '"' + (c.op === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select>" +
        (["exists", "missing"].includes(c.op) ? "<span></span>" : '<input data-k="value" placeholder="' + ({ status: "200 or 2xx", time: "500", type: "string" }[c.kind === "json" && c.op === "type" ? "type" : c.kind] || "value") +
          '" value="' + esc(c.value ?? "") + '" spellcheck="false">') +
        '<button class="icon-btn" data-ckdel="' + i + '" title="Remove">' + icon("close") + "</button></div>").join("") + "</div>" +
        '<div class="row tight"><button class="btn small" data-b="addcheck">' + icon("plus") + 'Add check</button>' +
        (A.res && A.res.json != null ? '<button class="btn small" data-b="suggestchecks">' + icon("spark") + "Checks from this response</button>" : "") + "</div>" +
        '<p class="faint tiny">Checks run every time you send this request and when its collection runs. JSON paths look like <code>data.items[0].id</code> or <code>total</code>.</p>';
    }
  }
  function statusClass(s) { return !s ? "bad" : s < 300 ? "ok" : s < 400 ? "warn" : "bad"; }
  function fmtSize(n) { return n == null ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
  function highlightJSON(text) {
    return esc(text).replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      (m, str, colon, kw, num) => str ? '<span class="' + (colon ? "jk" : "js") + '">' + str + "</span>" + (colon || "") : kw ? '<span class="jb">' + kw + "</span>" : '<span class="jn">' + num + "</span>");
  }
  function renderRes() {
    const res = A.res, box = $("#resBox");
    if (!res) { box.innerHTML = '<div class="res-empty faint">Send a request to see the response here. Ctrl+Enter sends.</div>'; return; }
    if (res.pending) { box.innerHTML = '<div class="res-empty"><span class="spinner"></span> Sending…</div>'; return; }
    const checks = res.checks || [];
    const passed = checks.filter(c => c.pass).length;
    let h = '<div class="res-head">';
    if (res.error) h += '<span class="pill bad">Error</span><span class="res-err">' + esc(res.error) + "</span>";
    else h += '<span class="pill ' + statusClass(res.status) + '">' + esc(res.status + " " + (res.reason || "")) + '</span><span class="faint small">' + esc(res.ms + " ms · " + fmtSize(res.size)) + "</span>";
    if (checks.length) h += '<span class="pill ' + (passed === checks.length ? "ok" : "bad") + '">' + icon(passed === checks.length ? "check" : "cross") + passed + "/" + checks.length + " checks</span>";
    if (res.missing && res.missing.length) h += '<span class="pill warn" title="Set these in an environment">Unknown: ' + esc(res.missing.map(m => "{{" + m + "}}").join(" ")) + "</span>";
    h += '<span class="res-acts"><button class="btn small" data-r="ai">' + icon("spark") + "Ask AI</button><button class=\"btn small\" data-r=\"curl\">" + icon("copy") + "Copy as curl</button></span></div>";
    if (!res.error) {
      h += '<div class="tabs res-tabs">' + [["body", "Body"], ["headers", "Headers (" + (res.headers || []).length + ")"], ["checks", "Checks"], ["sent", "Request sent"]].map(([k, l]) =>
        '<button class="tab' + (A.resTab === k ? " active" : "") + '" data-rt="' + k + '">' + l + "</button>").join("") +
        (A.resTab === "body" && res.json != null ? '<label class="check tiny"><input type="checkbox" id="resRaw"' + (A.raw ? " checked" : "") + "> raw</label>" : "") + "</div>";
      let body = "";
      if (A.resTab === "body") {
        if (res.body_b64 && /^image\//.test(res.content_type || "")) body = '<img class="res-img" src="data:' + esc(res.content_type) + ";base64," + res.body_b64 + '" alt="">';
        else if (res.body_b64) body = '<p class="faint small">Binary response (' + esc(res.content_type || "data") + ", " + fmtSize(res.size) + ").</p>";
        else if (res.json != null && !A.raw) body = '<pre class="res-pre json">' + highlightJSON(JSON.stringify(res.json, null, 2)) + "</pre>";
        else body = '<pre class="res-pre">' + esc(res.body || "(empty body)") + "</pre>";
        if (res.truncated) body += '<p class="faint tiny">Only the first 5 MB is shown.</p>';
      } else if (A.resTab === "headers") {
        body = '<table class="table hdrs">' + (res.headers || []).map(([k, v]) => "<tr><td>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("") + "</table>";
      } else if (A.resTab === "checks") {
        body = checks.length ? '<div class="ckres">' + checks.map(c => '<div class="' + (c.pass ? "ok" : "bad") + '">' + icon(c.pass ? "check" : "cross") + "<span>" + esc(c.label) +
          '</span><span class="faint">got ' + esc(c.actual) + "</span></div>").join("") + "</div>" : '<p class="faint small">No checks on this request. Add some on the Checks tab.</p>';
      } else {
        body = '<pre class="res-pre">' + esc(res.method + " " + res.url + "\n" + (res.sent_headers || []).map(([k, v]) => k + ": " + v).join("\n")) + "</pre>";
      }
      h += '<div class="res-body">' + body + "</div>";
    }
    box.innerHTML = h;
  }

  // ── editing ──────────────────────────────────────────────────────────
  function touch() { A.dirty = true; $("#reqWhere").textContent = $("#reqWhere").textContent.replace(/ · unsaved changes$/, "") + (A.colId ? " · unsaved changes" : ""); }
  function confirmDiscard() { return !A.dirty || !A.colId || confirm("Discard unsaved changes to " + A.req.name + "?"); }
  function newRequest(colId) {
    if (!confirmDiscard()) return;
    A.req = blank();
    A.colId = colId || "";
    A.dirty = !!colId;
    A.res = null;
    A.reqTab = "params";
    if (colId) saveRequest();
    if ($("#view-api").hidden === false) { render(); $("#reqUrl").focus(); }
  }
  function loadRequest(colId, r) {
    if (!confirmDiscard()) return;
    A.req = clone(r);
    A.colId = colId;
    A.dirty = false;
    A.res = null;
    render();
  }
  async function saveRequest() {
    let col = A.scope.collections.find(c => c.id === A.colId);
    if (!col) {
      const cols = A.scope.collections;
      const choice = await pickCollection(cols);
      if (choice === null) return;
      if (choice === "") {
        col = (await api("POST", "http/collections", { project: A.project, name: A.project ? "Requests" : "My requests" })).collection;
      } else col = cols.find(c => c.id === choice);
      A.colId = col.id;
    }
    const reqs = col.requests.filter(r => r.id !== A.req.id);
    const idx = col.requests.findIndex(r => r.id === A.req.id);
    reqs.splice(idx >= 0 ? idx : reqs.length, 0, clone(A.req));
    const saved = (await api("PUT", "http/collections/" + col.id, { requests: reqs })).collection;
    Object.assign(col, saved);
    A.dirty = false;
    A.open.add(col.id);
    renderSide();
    renderReq();
    toast("Saved to " + col.name);
  }
  function pickCollection(cols) {
    return new Promise(resolve => {
      const d = openModal("<h3>Save request</h3><p class=\"faint small\">Pick a collection for it.</p><div class=\"opts\">" +
        cols.map((c, i) => '<label class="check"><input type="radio" name="pc" value="' + esc(c.id) + '"' + (i ? "" : " checked") + "> " + esc(c.name) + (c.project ? "" : ' <span class="faint">(global)</span>') + "</label>").join("") +
        '<label class="check"><input type="radio" name="pc" value=""' + (cols.length ? "" : " checked") + "> A new collection</label></div>" +
        '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Save</button></div>');
      d.querySelector('[data-a="cancel"]').onclick = () => { closeModal(); resolve(null); };
      d.querySelector('[data-a="ok"]').onclick = () => { const v = (d.querySelector("input[name=pc]:checked") || {}).value; closeModal(); resolve(v ?? ""); };
    });
  }
  function readKV(container, list) {
    const rows = Array.from(container.querySelectorAll(".kvr"));
    const out = [];
    rows.forEach(row => {
      const key = row.querySelector('[data-f="key"]').value, value = row.querySelector('[data-f="value"]').value;
      if (row.classList.contains("new") && !key && !value) return;
      out.push({ key, value, enabled: row.querySelector('[data-f="enabled"]').checked || row.classList.contains("new") });
    });
    list.length = 0;
    out.forEach(x => list.push(x));
  }
  function kvTarget(kind) { return kind === "form" ? A.req.body.form : A.req[kind]; }

  async function send() {
    const r = A.req;
    A.res = { pending: true };
    A.resTab = A.resTab === "sent" ? "body" : A.resTab;
    renderRes();
    try {
      A.res = await api("POST", "http/send", { project: A.project, request: r });
      if (!A.res.error && A.res.checks && A.res.checks.some(c => !c.pass) && A.resTab === "body") A.resTab = "checks";
    } catch (e) { A.res = { error: e.message }; }
    renderRes();
    renderReq();
    await load();
    if (A.sideTab === "history") renderSide();
  }
  function checksFromResponse() {
    const res = A.res, out = [{ kind: "status", op: "eq", value: String(res.status), enabled: true }];
    const walk = (v, path, depth) => {
      if (depth > 2 || out.length > 8) return;
      if (Array.isArray(v)) { out.push({ kind: "json", op: "type", path: path || "$", value: "array", enabled: true }); if (v.length) walk(v[0], path + "[0]", depth + 1); }
      else if (v && typeof v === "object") Object.keys(v).slice(0, 6).forEach(k => {
        const p = path ? path + "." + k : k;
        if (v[k] !== null && typeof v[k] === "object") walk(v[k], p, depth + 1);
        else out.push({ kind: "json", op: "type", path: p, value: v[k] === null ? "null" : typeof v[k], enabled: true });
      });
    };
    walk(res.json, "", 0);
    return out;
  }

  // ── dialogs ──────────────────────────────────────────────────────────
  function envDialog(selectId) {
    const envs = A.scope.environments;
    let cur = envs.find(e => e.id === (selectId || $("#apiEnv").value)) || envs[0] || null;
    const draw = () => {
      const d = openModal("<h3>Environments</h3><p class=\"faint small\">Variables you use as {{name}} in URLs, headers, bodies and auth. Switch environments to point the same requests at " +
        "your laptop, staging or production. Values marked <b>secret</b> are hidden and never shown to the AI.</p>" +
        '<div class="row tight">' + envs.map(e => '<button class="btn small' + (cur && e.id === cur.id ? " on" : "") + '" data-env="' + esc(e.id) + '">' + esc(e.name) + "</button>").join("") +
        '<button class="btn small" data-a="new">' + icon("plus") + "New</button></div>" +
        (cur ? '<label>Name<input id="envName" value="' + esc(cur.name) + '"></label><div class="envvars">' + cur.vars.concat([{ key: "", value: "", secret: false, enabled: true, _new: true }]).map((v, i) =>
          '<div class="ev' + (v._new ? " new" : "") + '"><input data-v="key" placeholder="name, e.g. baseUrl" value="' + esc(v.key) + '" spellcheck="false">' +
          '<input data-v="value" type="' + (v.secret ? "password" : "text") + '" placeholder="value" value="' + esc(v.value) + '" spellcheck="false">' +
          '<label class="check tiny"><input type="checkbox" data-v="secret"' + (v.secret ? " checked" : "") + "> secret</label>" +
          (v._new ? "<span></span>" : '<button class="icon-btn" data-evdel="' + i + '">' + icon("close") + "</button>") + "</div>").join("") + "</div>" +
          '<div class="row between"><button class="btn danger" data-a="delete">Delete environment</button><span><button class="btn" data-a="cancel">Close</button> <button class="btn primary" data-a="save">Save</button></span></div>'
          : '<p class="faint small">No environments yet. Press New: it starts with {{baseUrl}}.</p><div class="row end"><button class="btn" data-a="cancel">Close</button></div>'));
      d.classList.add("wide");
      const collect = () => {
        if (!cur) return;
        cur.name = $("#envName").value;
        cur.vars = Array.from(d.querySelectorAll(".ev")).map(row => ({ key: row.querySelector('[data-v="key"]').value.trim(), value: row.querySelector('[data-v="value"]').value,
          secret: row.querySelector('[data-v="secret"]').checked, enabled: true })).filter(v => v.key);
      };
      d.querySelector('[data-a="cancel"]').onclick = () => { closeModal(); render(); };
      d.querySelectorAll("[data-env]").forEach(b => (b.onclick = () => { collect(); cur = envs.find(e => e.id === b.dataset.env); draw(); }));
      d.querySelector('[data-a="new"]').onclick = guard(async () => {
        collect();
        const name = prompt("Name for the environment (e.g. Local, Staging):", envs.length ? "Staging" : "Local");
        if (!name) return;
        const e = (await api("POST", "http/environments", { project: A.project, name })).environment;
        envs.push(e);
        A.scope.active_env = e.id;
        cur = e;
        draw();
      });
      d.querySelectorAll("[data-evdel]").forEach(b => (b.onclick = () => { collect(); cur.vars.splice(+b.dataset.evdel, 1); draw(); }));
      d.querySelectorAll('.ev.new input[data-v="key"]').forEach(inp => (inp.oninput = () => { collect(); draw(); const last = $$(".ev input[data-v=key]"); const el = last[last.length - 2]; if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }));
      d.querySelectorAll('[data-v="secret"]').forEach(cb => (cb.onchange = () => { cb.closest(".ev").querySelector('[data-v="value"]').type = cb.checked ? "password" : "text"; }));
      const save = d.querySelector('[data-a="save"]');
      if (save) save.onclick = guard(async () => { collect(); await api("PUT", "http/environments/" + cur.id, { name: cur.name, vars: cur.vars }); toast("Environment saved"); closeModal(); await load(); render(); });
      const del = d.querySelector('[data-a="delete"]');
      if (del) del.onclick = guard(async () => {
        if (!confirm("Delete the environment " + cur.name + "?")) return;
        await api("DELETE", "http/environments/" + cur.id);
        envs.splice(envs.indexOf(cur), 1);
        cur = envs[0] || null;
        draw();
      });
    };
    draw();
  }
  function importDialog() {
    const cols = A.scope.collections;
    const d = openModal("<h3>Import</h3><p class=\"faint small\">Paste a <b>curl</b> command (e.g. from your browser's dev tools: Copy as cURL) or a <b>Postman</b> collection, or choose a Postman .json file.</p>" +
      '<textarea id="impText" rows="8" spellcheck="false" placeholder="curl -X POST https://… -H \'Content-Type: application/json\' -d \'{…}\'"></textarea>' +
      '<div class="row tight"><input type="file" id="impFile" accept=".json,application/json"></div>' +
      (cols.length ? '<label>Put a curl request in<select id="impCol"><option value="">A new "Imported" collection</option>' + cols.map(c => '<option value="' + esc(c.id) + '">' + esc(c.name) + "</option>").join("") + "</select></label>" : "") +
      '<p class="small" id="impMsg"></p><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Import</button></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    $("#impFile").onchange = async e => { const f = e.target.files[0]; if (f) $("#impText").value = await f.text(); };
    d.querySelector('[data-a="ok"]').onclick = guard(async () => {
      try {
        const r = await api("POST", "http/import", { project: A.project, text: $("#impText").value, collection: ($("#impCol") || {}).value || "" });
        closeModal();
        await load();
        A.open.add(r.collection.id);
        A.sideTab = "collections";
        if (r.request) loadRequest(r.collection.id, r.request);
        else render();
        toast(r.request ? "Imported " + r.request.name : "Imported " + r.collection.name + " (" + r.collection.requests.length + " requests)" + (r.environment ? " and its variables" : ""));
      } catch (e) { $("#impMsg").textContent = e.message; $("#impMsg").style.color = "var(--red)"; }
    });
  }
  async function runCollection(col) {
    const d = openModal("<h3>Running " + esc(col.name) + '</h3><div class="res-empty"><span class="spinner"></span> Sending ' + col.requests.length + " requests…</div>");
    let r;
    try { r = await api("POST", "http/collections/" + col.id + "/run", { project: A.project }); }
    catch (e) { closeModal(); throw e; }
    const ok = r.results.filter(x => x.pass).length;
    d.innerHTML = "<h3>" + esc(col.name) + ': <span class="' + (ok === r.results.length ? "okc" : "badc") + '">' + ok + " of " + r.results.length + " passed</span></h3>" +
      '<div class="runres">' + r.results.map(x => '<div class="' + (x.pass ? "ok" : "bad") + '">' + icon(x.pass ? "check" : "cross") + '<b class="m m-' + esc(x.method || "GET") + '">' + esc((x.method || "").slice(0, 4)) +
        "</b><span>" + esc(x.name) + '</span><span class="faint small">' + esc(x.error ? x.error : x.status + " · " + x.ms + " ms") + "</span>" +
        (x.pass ? "" : '<div class="why">' + x.checks.filter(c => !c.pass).map(c => esc(c.label) + ": got " + esc(c.actual)).join("<br>") + "</div>") + "</div>").join("") + "</div>" +
      (A.project ? '<p class="faint small">Also recorded on the project\'s Tests tab.</p>' : "") +
      '<div class="row end">' + (ok < r.results.length ? '<button class="btn" data-a="ai">' + icon("spark") + "Ask the AI to fix the failures</button>" : "") + '<button class="btn primary" data-a="close">Close</button></div>';
    d.classList.add("wide");
    d.querySelector('[data-a="close"]').onclick = closeModal;
    const ai = d.querySelector('[data-a="ai"]');
    if (ai) ai.onclick = guard(async () => {
      closeModal();
      const fails = r.results.filter(x => !x.pass).map(x => "- " + x.name + " (" + x.method + " " + x.url + "): " + (x.error || x.checks.filter(c => !c.pass).map(c => c.label + ", got " + c.actual).join("; "))).join("\n");
      await askAI("Running the API collection \"" + col.name + "\" failed for these requests:\n" + fails + "\n\nFind out why (look at the endpoint code and the server logs) and fix it, then run the collection again with run_api_collection.");
    });
  }
  async function compareDialog() {
    const files = (await api("GET", "http/docs?" + q())).files;
    if (!files.length) { toast("This project has no documentation files (.md, .txt, .rst) to compare with.", true); return; }
    const d = openModal("<h3>Compare endpoints with the docs</h3><p class=\"faint small\">Pick the documentation that lists your API. Endpoints are matched by method and path " +
      "(/users/:id, /users/{id} and /users/&lt;id&gt; count as the same).</p><div class=\"opts\">" + files.map((f, i) => '<label class="check"><input type="checkbox" value="' + esc(f) + '"' +
        (i === 0 ? " checked" : "") + "> " + esc(f) + "</label>").join("") + '</div><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Compare</button></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="ok"]').onclick = guard(async () => {
      const picked = Array.from(d.querySelectorAll("input:checked")).map(i => i.value);
      const r = await api("POST", "http/compare", { project: A.project, files: picked });
      const list = (arr, empty, fmt) => arr.length ? "<ul>" + arr.map(fmt).join("") + "</ul>" : '<p class="faint small">' + empty + "</p>";
      const dd = openModal("<h3>Endpoints and docs</h3><p class=\"small\">" + r.documented.length + " of " + (r.documented.length + r.undocumented.length) + " endpoints in the code are documented; the docs mention " + r.doc_count + ".</p>" +
        "<h4>In the code, missing from the docs</h4>" + list(r.undocumented, "None: every endpoint is documented.", x => "<li><code>" + esc(x.method + " " + x.path) + '</code> <span class="faint">' + esc(x.file + ":" + x.line) + "</span></li>") +
        "<h4>In the docs, not in the code</h4>" + list(r.stale, "None.", x => "<li><code>" + esc(x.method + " " + x.path) + '</code> <span class="faint">' + esc(x.file + ":" + x.line) + "</span></li>") +
        '<div class="row end">' + (r.undocumented.length || r.stale.length ? '<button class="btn" data-a="sug">Add to the Tests list</button><button class="btn" data-a="ai">' + icon("spark") + "Ask the AI to update the docs</button>" : "") +
        '<button class="btn primary" data-a="close">Close</button></div>');
      dd.classList.add("wide");
      dd.querySelector('[data-a="close"]').onclick = closeModal;
      const sug = dd.querySelector('[data-a="sug"]');
      if (sug) sug.onclick = guard(async () => { await api("POST", "http/compare", { project: A.project, files: picked, suggest: true }); closeModal(); toast("Added to the project's Tests list"); });
      const ai = dd.querySelector('[data-a="ai"]');
      if (ai) ai.onclick = guard(async () => {
        closeModal();
        await askAI("Update the API documentation in " + picked.join(", ") + " so it matches the code. Use list_endpoints and read each endpoint's code to document its method, path, " +
          "parameters, request body and responses accurately. Missing from the docs: " + (r.undocumented.map(x => x.method + " " + x.path).join(", ") || "none") +
          ". In the docs but not in the code: " + (r.stale.map(x => x.method + " " + x.path).join(", ") || "none") + ". If a documentation standard applies, follow it.");
      });
    });
  }
  async function askAI(text) {
    if (A.project && app.Projects) {
      await app.newChat();
      await app.patchChat({ project: A.project });
    } else await app.newChat();
    app.renderChat();
    app.renderRail();
    $("#prompt").value = text;
    app.autosize();
    $("#prompt").focus();
  }

  // ── events from the server ───────────────────────────────────────────
  function onEvent(ev) {
    if (ev.project !== A.project || !A.server) return;
    if (ev.type === "server_log") {
      A.log = (A.log + ev.data).slice(-200000);
      const l = $("#srvLog");
      if (l) { const stick = l.scrollHeight - l.scrollTop - l.clientHeight < 40; l.textContent = A.log.slice(-40000); if (stick) l.scrollTop = l.scrollHeight; }
    } else if (ev.type === "server") {
      A.server.state = ev.state;
      if (ev.code !== undefined) A.server.code = ev.code;
      if ($("#view-api").hidden === false) renderServer();
      if (ev.state === "running") toast("Dev server is running" + (ev.note ? " (" + ev.note + ")" : ""));
      if (ev.state === "exited") toast("The dev server stopped. Open its Log to see why.", true);
    }
  }

  // ── wiring ───────────────────────────────────────────────────────────
  function wire() {
    $("#reqMethod").innerHTML = METHODS.map(m => '<option value="' + m + '">' + m + "</option>").join("");
    $("#apiProject").onchange = guard(async () => {
      if (!confirmDiscard()) { $("#apiProject").value = A.project; return; }
      A.project = $("#apiProject").value;
      store("va-api-project", A.project);
      A.routes = null;
      A.colId = "";
      A.req = blank();
      A.res = null;
      A.dirty = false;
      await load();
      render();
    });
    $("#apiEnv").onchange = guard(async () => { await api("POST", "http/active_env", { project: A.project, env: $("#apiEnv").value }); A.scope.active_env = $("#apiEnv").value; });
    $("#apiEnvEdit").onclick = () => envDialog();
    $("#apiImport").onclick = importDialog;
    $("#apiNew").onclick = () => newRequest();
    $("#reqMethod").onchange = () => { A.req.method = $("#reqMethod").value; $("#reqMethod").className = "m-" + A.req.method; touch(); };
    $("#reqUrl").oninput = () => {
      // pasting a full URL with ?a=b moves the query into Params
      let v = $("#reqUrl").value;
      if (v.startsWith("curl ")) return;
      const i = v.indexOf("?");
      if (i >= 0 && !v.slice(i).includes("{{")) {
        const qs = new URLSearchParams(v.slice(i + 1));
        qs.forEach((value, key) => A.req.params.push({ key, value, enabled: true }));
        v = v.slice(0, i);
        $("#reqUrl").value = v;
        A.reqTab = "params";
        renderReq();
      }
      A.req.url = v;
      touch();
    };
    $("#reqUrl").onpaste = e => {
      const t = (e.clipboardData || window.clipboardData).getData("text");
      if (/^\s*curl\s/.test(t)) {
        e.preventDefault();
        guard(async () => { const r = await api("POST", "http/import", { project: A.project, text: t, collection: A.colId }); await load(); A.open.add(r.collection.id); loadRequest(r.collection.id, r.request); toast("Imported the curl command"); })();
      }
    };
    $("#reqName").oninput = () => { A.req.name = $("#reqName").value; touch(); };
    $("#reqSend").onclick = guard(send);
    $("#reqSave").onclick = guard(saveRequest);
    $("#view-api").addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); guard(send)(); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); guard(saveRequest)(); }
    });
    $$(".req-tabs .tab").forEach(t => (t.onclick = () => { A.reqTab = t.dataset.rtab; renderReq(); }));
    $$(".api-side .tab").forEach(t => (t.onclick = guard(async () => {
      A.sideTab = t.dataset.aside;
      if (A.sideTab === "endpoints" && !A.routes && A.project) { renderSide(); A.routes = (await api("GET", "http/endpoints?" + q())).routes; }
      renderSide();
    })));
    const pane = $("#reqPane");
    pane.addEventListener("input", e => {
      const kv = e.target.closest(".kv");
      if (kv) {
        const wasNew = e.target.closest(".kvr").classList.contains("new");
        readKV(kv, kvTarget(kv.dataset.kv));
        touch();
        if (wasNew) {
          const f = e.target.dataset.f, idx = +e.target.closest(".kvr").dataset.i;
          renderReq();
          const el = $('#reqPane .kvr[data-i="' + idx + '"] [data-f="' + f + '"]');
          if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
        }
        return;
      }
      if (e.target.id === "bodyText") { A.req.body.text = e.target.value; touch(); return; }
      if (e.target.dataset.auth) { A.req.auth[e.target.dataset.auth] = e.target.value; touch(); return; }
      const ck = e.target.closest(".ck");
      if (ck && e.target.dataset.k && e.target.tagName === "INPUT" && e.target.type !== "checkbox") { A.req.checks[+ck.dataset.ck][e.target.dataset.k] = e.target.value; touch(); }
    });
    pane.addEventListener("change", e => {
      const kv = e.target.closest(".kv");
      if (kv) { readKV(kv, kvTarget(kv.dataset.kv)); touch(); return; }
      if (e.target.name === "bmode") {
        A.req.body.mode = e.target.value;
        if (e.target.value === "json" && !A.req.body.text) A.req.body.text = "{\n  \n}";
        touch();
        renderReq();
        return;
      }
      if (e.target.dataset.auth) { A.req.auth[e.target.dataset.auth] = e.target.value; touch(); renderReq(); return; }
      const ck = e.target.closest(".ck");
      if (ck) {
        const c = A.req.checks[+ck.dataset.ck], k = e.target.dataset.k;
        c[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
        if (k === "kind") { c.op = CHECK_OPS[c.kind][0][0]; c.value = c.kind === "status" ? "200" : c.kind === "time" ? "500" : ""; }
        touch();
        renderReq();
      }
    });
    pane.addEventListener("click", e => {
      const del = e.target.closest("[data-kvdel]");
      if (del) { const kv = del.closest(".kv"); kvTarget(kv.dataset.kv).splice(+del.dataset.kvdel, 1); touch(); renderReq(); return; }
      const cd = e.target.closest("[data-ckdel]");
      if (cd) { A.req.checks.splice(+cd.dataset.ckdel, 1); touch(); renderReq(); return; }
      const b = e.target.closest("[data-b]");
      if (!b) return;
      if (b.dataset.b === "format") {
        try { A.req.body.text = JSON.stringify(JSON.parse(A.req.body.text), null, 2); renderReq(); } catch (err) { toast("That isn't valid JSON: " + err.message, true); }
      } else if (b.dataset.b === "addcheck") { A.req.checks.push({ kind: "json", op: "exists", path: "", value: "", enabled: true }); touch(); renderReq(); }
      else if (b.dataset.b === "suggestchecks") { A.req.checks = checksFromResponse(); touch(); renderReq(); toast("Added checks that match this response. Adjust them as you like."); }
    });
    $("#resBox").addEventListener("click", guard(async e => {
      const t = e.target.closest("[data-rt]");
      if (t) { A.resTab = t.dataset.rt; renderRes(); return; }
      if (e.target.id === "resRaw") { A.raw = e.target.checked; renderRes(); return; }
      const b = e.target.closest("[data-r]");
      if (!b) return;
      if (b.dataset.r === "curl") {
        const r = await api("POST", "http/curl", { project: A.project, request: A.req });
        await navigator.clipboard.writeText(r.curl);
        toast("Copied the curl command");
      } else if (b.dataset.r === "ai") {
        const res = A.res;
        const body = res.json != null ? JSON.stringify(res.json, null, 2) : (res.body || "");
        await askAI("I sent " + A.req.method + " " + (res.url || A.req.url) + " and got " + (res.error ? "an error: " + res.error : res.status + " " + (res.reason || "") + " in " + res.ms + " ms") +
          ".\n" + (res.checks && res.checks.some(c => !c.pass) ? "Failed checks: " + res.checks.filter(c => !c.pass).map(c => c.label + " (got " + c.actual + ")").join("; ") + "\n" : "") +
          (body ? "Response body:\n```\n" + body.slice(0, 2500) + "\n```\n" : "") + "\nExplain what this means" + (A.project ? " and, if something is wrong, find the cause in the code and fix it" : "") + ".");
      }
    }));
    $("#apiSide").addEventListener("click", guard(async e => {
      const b = e.target.closest("[data-c]");
      const colEl = e.target.closest("[data-col]");
      const col = colEl && A.scope.collections.find(c => c.id === colEl.dataset.col);
      if (b) {
        const a = b.dataset.c;
        if (a === "toggle") { A.open.has(col.id) ? A.open.delete(col.id) : A.open.add(col.id); renderSide(); }
        else if (a === "newcol") {
          const name = prompt("Name for the collection:", "Users API");
          if (!name) return;
          const c = (await api("POST", "http/collections", { project: A.project, name })).collection;
          A.scope.collections.push(c);
          A.open.add(c.id);
          renderSide();
        } else if (a === "run") await runCollection(col);
        else if (a === "add") { A.open.add(col.id); newRequest(col.id); }
        else if (a === "more") {
          const d = openModal("<h3>" + esc(col.name) + '</h3><label>Name<input id="colName" value="' + esc(col.name) + '"></label>' +
            '<div class="row between"><button class="btn danger" data-a="del">Delete collection</button><span><button class="btn" data-a="export">Export (Postman)</button> ' +
            '<button class="btn primary" data-a="save">Save</button></span></div>');
          d.querySelector('[data-a="save"]').onclick = guard(async () => { await api("PUT", "http/collections/" + col.id, { name: $("#colName").value }); closeModal(); await load(); renderSide(); });
          d.querySelector('[data-a="export"]').onclick = () => { app.download("http/collections/" + col.id + "/export"); closeModal(); };
          d.querySelector('[data-a="del"]').onclick = guard(async () => {
            if (!confirm("Delete " + col.name + " and its " + col.requests.length + " requests?")) return;
            await api("DELETE", "http/collections/" + col.id);
            if (A.colId === col.id) { A.colId = ""; A.dirty = false; }
            closeModal(); await load(); render();
          });
        } else if (a === "clearhist") { if (confirm("Clear the request history?")) { await api("DELETE", "http/history?" + q()); await load(); renderSide(); } }
        else if (a === "scan") { A.routes = null; renderSide(); A.routes = (await api("GET", "http/endpoints?" + q())).routes; renderSide(); toast("Found " + A.routes.length + " endpoints"); }
        else if (a === "addall") {
          const reqs = A.routes.map(r => endpointRequest(r));
          const c = (await api("POST", "http/collections", { project: A.project, name: "Endpoints", requests: reqs })).collection;
          A.scope.collections.push(c);
          A.open.add(c.id);
          A.sideTab = "collections";
          render();
          toast("Added " + reqs.length + " requests to the Endpoints collection. Fill in path values and bodies, then run it.");
        } else if (a === "compare") await compareDialog();
        return;
      }
      const rq = e.target.closest("[data-rq]");
      if (rq) { const r = col.requests.find(x => x.id === rq.dataset.rq); if (r) loadRequest(col.id, r); return; }
      const hi = e.target.closest("[data-hist]");
      if (hi) {
        const x = A.scope.history.find(y => y.id === hi.dataset.hist);
        if (x && confirmDiscard()) { A.req = Object.assign(blank(), clone(x.request), { id: uid(), checks: [] }); A.colId = ""; A.dirty = false; A.res = null; render(); }
        return;
      }
      const ep = e.target.closest("[data-ep]");
      if (ep && confirmDiscard()) { A.req = endpointRequest(A.routes[+ep.dataset.ep]); A.colId = ""; A.dirty = false; A.res = null; render(); $("#reqUrl").focus(); }
    }));
    $("#apiServer").addEventListener("click", guard(async e => {
      const b = e.target.closest("[data-srv]");
      if (!b) return;
      const a = b.dataset.srv;
      if (a === "log") { A.showLog = !A.showLog; renderServer(); return; }
      const body = { project: A.project, action: a === "restart" ? "start" : a, command: $("#srvCmd").value, url: $("#srvUrl").value };
      if (a !== "stop") { A.log = ""; A.showLog = true; }
      A.server = Object.assign(A.server || {}, await api("POST", "http/server", body));
      renderServer();
    }));
    $("#apiServer").addEventListener("change", guard(async e => {
      if (e.target.id === "srvCmd" || e.target.id === "srvUrl") {
        const r = await api("POST", "http/server", { project: A.project, action: "save", command: $("#srvCmd").value, url: $("#srvUrl").value });
        A.server.config = r.config;
      }
    }));
  }
  function endpointRequest(r) {
    const path = r.path.replace(/:(\w+)|\{(\w+)(?::[^}]*)?\}|<(?:\w+:)?(\w+)>/g, (m, a, b, c) => "{{" + (a || b || c) + "}}");
    const method = r.method === "ANY" ? "GET" : r.method;
    const req = blank(method + " " + r.path, method, "{{baseUrl}}" + (path.startsWith("/") ? path : "/" + path));
    if (["POST", "PUT", "PATCH"].includes(method)) req.body = { mode: "json", text: "{\n  \n}", form: [] };
    return req;
  }

  wire();
  return { show, onEvent, leaving: confirmDiscard };
};
