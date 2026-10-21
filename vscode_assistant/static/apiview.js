// The API tab, at Postman level: collections with folders, tabs, every body and auth type, pre-request and
// test scripts (pm.*), checks, environments/globals, cookies, a collection runner with reports, code
// snippets, OpenAPI/Postman/curl import, WebSocket and SSE, plus the dev server and endpoint checks.
// app.js calls VAApi(app) once.
window.VAApi = function (app) {
  "use strict";
  const { api, toast, guard, openModal, closeModal, $, $$ } = app;
  const esc = v => app.esc(v == null ? "" : String(v));
  const A = {
    project: "", scope: null, server: null, tasks: null, node: false, tabs: [], active: -1, sideTab: "collections",
    routes: null, open: new Set(), showLog: false, log: "", filter: "", runs: {}, rtEarly: {},
  };
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { /* private window */ } return null; };
  const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];
  const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(6)), b => b.toString(16).padStart(2, "0")).join("");
  const clone = o => JSON.parse(JSON.stringify(o));
  const q = () => "project=" + encodeURIComponent(A.project);
  const blank = (name, method, url) => ({ id: uid(), name: name || "New request", method: method || "GET", url: url == null ? "{{baseUrl}}/" : url,
    params: [], path_vars: [], headers: [], body: { mode: "none", text: "", form: [] }, auth: { type: "inherit" }, checks: [],
    pre_script: "", test_script: "", settings: {}, examples: [] });
  const tab = () => A.tabs[A.active] || null;
  const cols = () => (A.scope ? A.scope.collections : []);
  const colById = id => cols().find(c => c.id === id);

  // ── the collection tree ──────────────────────────────────────────────
  function find(items, id, parent) {
    for (let i = 0; i < (items || []).length; i++) {
      const n = items[i];
      if (n.id === id) return { node: n, list: items, index: i, parent: parent || null };
      if (n.folder) { const r = find(n.items, id, n); if (r) return r; }
    }
    return null;
  }
  function pathTo(col, id) {
    const out = [];
    const walk = (items, trail) => {
      for (const n of items || []) {
        if (n.id === id) { out.push(...trail); return true; }
        if (n.folder && walk(n.items, trail.concat([n]))) return true;
      }
      return false;
    };
    walk(col.items, []);
    return out;
  }
  function allRequests(node) {
    const out = [];
    const walk = items => (items || []).forEach(n => (n.folder ? walk(n.items) : out.push(n)));
    walk(node.items);
    return out;
  }
  async function putCollection(col, fields) {
    const saved = (await api("PUT", "http/collections/" + col.id, Object.assign({ project: A.project }, fields || { items: col.items }))).collection;
    Object.assign(col, saved);
    return col;
  }

  // ── loading ──────────────────────────────────────────────────────────
  async function show() {
    const r = await api("GET", "projects");
    const sel = $("#apiProject");
    const prev = A.project || store("va-api-project") || "";
    sel.innerHTML = '<option value="">No project (global)</option>' + r.projects.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + "</option>").join("");
    A.project = r.projects.some(p => p.id === prev) ? prev : "";
    sel.value = A.project;
    await load();
    if (!A.tabs.length) newTab();
    renderAll();
  }
  async function load() {
    const s = await api("GET", "http/state?" + q());
    A.scope = s;
    A.server = s.server || null;
    A.tasks = s.tasks || null;
    A.node = s.node;
    A.log = (s.server && s.server.log) || A.log;
  }
  function renderAll() { renderEnv(); renderServer(); renderSide(); renderTabs(); renderEditor(); }

  function renderEnv() {
    const envs = A.scope.environments;
    $("#apiEnv").innerHTML = '<option value="">No environment</option>' + envs.map(e => '<option value="' + esc(e.id) + '">' + esc(e.name) +
      (e.project ? "" : " (global)") + "</option>").join("");
    $("#apiEnv").value = envs.some(e => e.id === A.scope.active_env) ? A.scope.active_env : "";
  }

  // ── dev server bar ───────────────────────────────────────────────────
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

  // ── side panel ───────────────────────────────────────────────────────
  const mshort = m => ({ DELETE: "DEL", OPTIONS: "OPT", PATCH: "PTCH" }[m] || m);
  function reqBadge(r) {
    if (r.kind === "ws") return '<b class="m m-WS">WS</b>';
    if (r.kind === "sse") return '<b class="m m-SSE">SSE</b>';
    return '<b class="m m-' + esc(r.method) + '">' + esc(mshort(r.method)) + "</b>";
  }
  function matches(n) {
    if (!A.filter) return true;
    const f = A.filter.toLowerCase();
    if (n.folder) return (n.name || "").toLowerCase().includes(f) || (n.items || []).some(matches);
    return (n.name || "").toLowerCase().includes(f) || (n.url || "").toLowerCase().includes(f);
  }
  function renderSide() {
    $$(".api-side .tab").forEach(t => t.classList.toggle("active", t.dataset.aside === A.sideTab));
    $("#apiEndpTab").hidden = !A.project;
    if (A.sideTab === "endpoints" && !A.project) A.sideTab = "collections";
    $(".api-filter").hidden = A.sideTab === "endpoints";
    const box = $("#apiSide");
    const t = tab();
    const activeNode = t && (t.nodeId || (t.req && t.req.id));
    if (A.sideTab === "collections") {
      const nodeHTML = (col, n, depth) => {
        if (!matches(n)) return "";
        const pad = 'style="padding-left:' + (8 + depth * 14) + 'px"';
        if (n.folder) {
          const open = A.open.has(n.id) || !!A.filter;
          return '<div class="tn fold' + (activeNode === n.id ? " active" : "") + '" draggable="true" data-col="' + esc(col.id) + '" data-node="' + esc(n.id) + '" ' + pad + ">" +
            icon(open ? "chevron-down" : "chevron-right", "tw") + icon("folder", "fi") + '<span class="nm">' + esc(n.name) + "</span>" +
            '<span class="acts"><button class="icon-btn" data-a="run" title="Run this folder">' + icon("play") + '</button><button class="icon-btn" data-a="add" title="New request here">' +
            icon("plus") + '</button><button class="icon-btn" data-a="more" title="More">' + icon("edit") + "</button></span></div>" +
            (open ? (n.items || []).map(x => nodeHTML(col, x, depth + 1)).join("") || '<p class="faint tiny" ' + pad.replace(/\d+px/, (8 + (depth + 1) * 14 + 18) + "px") + ">Empty folder</p>" : "");
        }
        return '<div class="tn rq' + (activeNode === n.id ? " active" : "") + '" draggable="true" data-col="' + esc(col.id) + '" data-node="' + esc(n.id) + '" ' + pad + ">" +
          '<span class="tw-gap"></span>' + reqBadge(n) + '<span class="nm">' + esc(n.name) + '</span><span class="acts"><button class="icon-btn" data-a="more" title="More">' + icon("edit") + "</button></span></div>";
      };
      const list = cols();
      box.innerHTML = list.length ? list.map(c => {
        const open = A.open.has(c.id) || !!A.filter;
        if (A.filter && !(c.items || []).some(matches) && !c.name.toLowerCase().includes(A.filter.toLowerCase())) return "";
        return '<div class="tn coll" data-col="' + esc(c.id) + '" data-node="' + esc(c.id) + '">' + icon(open ? "chevron-down" : "chevron-right", "tw") +
          '<span class="nm">' + esc(c.name) + "</span>" + (c.location === "project" ? '<span class="tag" title="Stored in ' + esc(c.file) + ' (shared through Git)">repo</span>' : c.project ? "" : '<span class="tag">global</span>') +
          (c.error ? '<span class="tag bad" title="' + esc(c.error) + '">error</span>' : "") +
          '<span class="acts"><button class="icon-btn" data-a="run" title="Run the collection">' + icon("play") + '</button><button class="icon-btn" data-a="add" title="New request">' +
          icon("plus") + '</button><button class="icon-btn" data-a="more" title="More">' + icon("edit") + "</button></span></div>" +
          (open ? (c.items || []).map(n => nodeHTML(c, n, 1)).join("") || '<p class="faint tiny pad">Empty. Press + to add a request.</p>' : "");
      }).join("") : '<div class="pad"><p class="faint small">No collections yet.</p><button class="btn small" data-a="newcol">' + icon("folder-plus") + 'New collection</button> <button class="btn small" data-a="import">Import</button></div>';
    } else if (A.sideTab === "history") {
      const h = A.scope.history.filter(x => !A.filter || (x.request.url || "").toLowerCase().includes(A.filter.toLowerCase()));
      box.innerHTML = '<div class="t-sec"><span>History</span>' + (h.length ? '<button class="icon-btn" data-a="clearhist" title="Clear the history">' + icon("clear") + "</button>" : "") + "</div>" +
        (h.length ? h.map(x => '<div class="tn hi" data-hist="' + esc(x.id) + '"><b class="m m-' + esc(x.request.method) + '">' + esc(mshort(x.request.method)) + '</b><span class="nm mono">' +
          esc(x.request.url) + '</span><span class="hs ' + (x.error || x.status >= 400 ? "bad" : "ok") + '">' + esc(x.error ? "error" : String(x.status)) + "</span>" +
          (x.by === "ai" ? '<span class="tag">AI</span>' : "") + "</div>").join("") : '<p class="faint small pad">Requests you and the AI send show up here.</p>');
    } else {
      const r = A.routes;
      box.innerHTML = '<div class="t-sec"><span>Endpoints in the code</span><span><button class="btn small" data-a="scan">' + icon("refresh") + "Scan</button></span></div>" +
        (r ? (r.length ? '<div class="row tight pad"><button class="btn small" data-a="addall">Add all to a collection</button><button class="btn small" data-a="compare">Compare with docs</button></div>' +
          r.map((x, i) => '<div class="tn ep" data-ep="' + i + '" title="' + esc(x.file + (x.line ? ":" + x.line : "")) + '"><b class="m m-' + esc(x.method) + '">' + esc(mshort(x.method)) +
            '</b><span class="nm mono">' + esc(x.path) + '</span><span class="faint tiny">' + esc(x.file.split("/").pop()) + "</span></div>").join("")
          : '<p class="faint small pad">No endpoints found. Supported: Express, FastAPI, Flask, Django, Spring, JAX-RS, ASP.NET, Go, Rails, Laravel and OpenAPI JSON files.</p>')
          : '<p class="faint small pad">Find the HTTP endpoints your code defines, try them, and check the docs list the same ones.</p>');
    }
  }

  // ── tabs ─────────────────────────────────────────────────────────────
  function newTab(req, colId, parent) {
    const t = { id: uid(), type: "request", req: req || blank(), colId: colId || "", parent: parent || "", dirty: !req, saved: !!colId,
      res: null, reqTab: "params", resTab: "body", scriptTab: "test", rt: null };
    if (!req) t.req.checks = [{ kind: "status", op: "eq", value: "2xx", enabled: true }];
    A.tabs.push(t);
    A.active = A.tabs.length - 1;
    return t;
  }
  function openNode(colId, nodeId) {
    const i = A.tabs.findIndex(t => (t.colId === colId && t.nodeId === nodeId) || (t.type === "request" && t.colId === colId && t.req.id === nodeId));
    if (i >= 0) { A.active = i; renderTabs(); renderEditor(); renderSide(); return; }
    const col = colById(colId);
    if (!col) return;
    if (colId === nodeId) {
      A.tabs.push({ id: uid(), type: "collection", colId, nodeId, node: clone(pick(col)), dirty: false, sub: "overview", scriptTab: "pre" });
    } else {
      const f = find(col.items, nodeId);
      if (!f) return;
      if (f.node.folder) A.tabs.push({ id: uid(), type: "folder", colId, nodeId, node: clone(pick(f.node)), dirty: false, sub: "auth", scriptTab: "pre" });
      else { newTab(clone(f.node), colId, f.parent ? f.parent.id : ""); A.tabs[A.tabs.length - 1].dirty = false; A.tabs[A.tabs.length - 1].saved = true; }
    }
    A.active = A.tabs.length - 1;
    // replace an untouched blank tab instead of piling up tabs
    const first = A.tabs[0];
    if (A.tabs.length > 1 && first.type === "request" && !first.saved && !first.res && first.req.url === "{{baseUrl}}/" && first.req.name === "New request") {
      A.tabs.splice(0, 1);
      A.active--;
    }
    renderTabs(); renderEditor(); renderSide();
  }
  const pick = n => ({ id: n.id, name: n.name, auth: n.auth || { type: n.folder ? "inherit" : "none" }, variables: n.variables || [], pre_script: n.pre_script || "",
    test_script: n.test_script || "", description: n.description || "", folder: !!n.folder });
  function tabTitle(t) {
    if (t.type === "request") return reqBadge(t.req) + "<span>" + esc(t.req.name) + "</span>";
    return icon(t.type === "folder" ? "folder" : "book") + "<span>" + esc(t.node.name) + "</span>";
  }
  function renderTabs() {
    $("#apiTabs").innerHTML = A.tabs.map((t, i) => '<div class="atab' + (i === A.active ? " active" : "") + '" data-tab="' + i + '" title="' + esc(t.type === "request" ? t.req.url : t.node.name) + '">' +
      tabTitle(t) + (t.dirty ? '<i class="dirty"></i>' : "") + '<button class="icon-btn" data-close="' + i + '" title="Close">' + icon("close") + "</button></div>").join("") +
      '<button class="icon-btn add-tab" data-a="newtab" title="New request (Ctrl+T)">' + icon("plus") + "</button>";
    const on = $("#apiTabs .atab.active");
    if (on) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function closeTab(i) {
    const t = A.tabs[i];
    if (t.dirty && t.saved && !confirm("Discard unsaved changes to " + (t.req ? t.req.name : t.node.name) + "?")) return;
    if (t.rt && t.rt.sid) api("POST", "http/rt", { action: "close", id: t.rt.sid }).catch(() => {});
    A.tabs.splice(i, 1);
    if (!A.tabs.length) newTab();
    A.active = Math.min(A.active >= i ? A.active - 1 : A.active, A.tabs.length - 1);
    if (A.active < 0) A.active = 0;
    renderTabs(); renderEditor(); renderSide();
  }
  function markDirty() {
    const t = tab();
    if (!t.dirty) { t.dirty = true; renderTabs(); }
  }

  // ── shared editor pieces ─────────────────────────────────────────────
  function kvRows(list, kind, opts) {
    opts = opts || {};
    const rows = list.concat(opts.noAdd ? [] : [{ key: "", value: "", enabled: true, _new: true }]);
    return '<div class="kv' + (opts.files ? " withtype" : "") + '" data-kv="' + kind + '">' + rows.map((r, i) => '<div class="kvr' + (r._new ? " new" : "") + '" data-i="' + i + '">' +
      (opts.noToggle ? "<span></span>" : '<input type="checkbox" data-f="enabled"' + (r.enabled !== false ? " checked" : "") + (r._new ? ' style="visibility:hidden"' : "") + ">") +
      '<input data-f="key" placeholder="' + (opts.keyPh || "Name") + '" value="' + esc(r.key || "") + '" spellcheck="false"' + (opts.fixedKeys ? " readonly" : "") + ">" +
      (opts.files ? '<select data-f="type"><option value="text"' + (r.type !== "file" ? " selected" : "") + '>Text</option><option value="file"' + (r.type === "file" ? " selected" : "") + ">File</option></select>" : "") +
      (opts.files && r.type === "file" ? '<span class="filecell"><button class="btn small" data-pickfile="' + i + '">' + icon("paperclip") + (r.file && r.file.name ? esc(r.file.name) : "Choose file") + "</button></span>"
        : '<input data-f="value" placeholder="' + (opts.valPh || "Value") + '" value="' + esc(r.value || "") + '" spellcheck="false"' + (opts.secretCol && r.secret ? ' type="password"' : "") + ">") +
      (opts.secretCol ? '<label class="check tiny"><input type="checkbox" data-f="secret"' + (r.secret ? " checked" : "") + "> secret</label>" : "") +
      (r._new || opts.fixedKeys ? "<span></span>" : '<button class="icon-btn" data-kvdel="' + i + '" title="Remove">' + icon("close") + "</button>") + "</div>").join("") + "</div>";
  }
  function readKV(container) {
    const out = [];
    container.querySelectorAll(":scope > .kvr").forEach(row => {
      const g = f => row.querySelector('[data-f="' + f + '"]');
      const key = g("key").value, value = g("value") ? g("value").value : "";
      if (row.classList.contains("new") && !key && !value) return;
      const r = { key, value, enabled: g("enabled") ? g("enabled").checked || row.classList.contains("new") : true };
      if (g("type")) r.type = g("type").value;
      if (g("secret")) r.secret = g("secret").checked;
      out.push(r);
    });
    return out;
  }
  const AUTH_TYPES = [["inherit", "Inherit from parent"], ["none", "No auth"], ["bearer", "Bearer token"], ["basic", "Basic auth"], ["apikey", "API key"],
    ["digest", "Digest auth"], ["oauth2", "OAuth 2.0"], ["awsv4", "AWS Signature"]];
  function authHTML(a, allowInherit, inheritedFrom) {
    a = a || { type: allowInherit ? "inherit" : "none" };
    const f = (k, l, ph, type) => '<label>' + l + '<input data-auth="' + k + '" type="' + (type || "text") + '" value="' + esc(a[k] || "") + '" placeholder="' + esc(ph || "") + '" spellcheck="false"></label>';
    const sel = (k, l, opts) => '<label>' + l + '<select data-auth="' + k + '">' + opts.map(([v, t]) => '<option value="' + v + '"' + ((a[k] || opts[0][0]) === v ? " selected" : "") + ">" + t + "</option>").join("") + "</select></label>";
    let h = '<div class="auth-form"><label>Type<select data-auth="type">' + AUTH_TYPES.filter(x => allowInherit || x[0] !== "inherit").map(([v, l]) =>
      '<option value="' + v + '"' + (a.type === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select></label>";
    if (a.type === "inherit") h += '<p class="faint small">' + esc(inheritedFrom || "Uses the auth of its folder or collection.") + "</p>";
    else if (a.type === "none") h += '<p class="faint small">No authorization is sent.</p>';
    else if (a.type === "bearer") h += f("token", "Token", "{{token}}");
    else if (a.type === "basic" || a.type === "digest") h += '<div class="grid2">' + f("username", "Username") + f("password", "Password", "{{password}}", "password") + "</div>";
    else if (a.type === "apikey") h += '<div class="grid2">' + f("key", "Key", "X-API-Key") + f("value", "Value", "{{apiKey}}") + "</div>" + sel("in", "Add to", [["header", "Header"], ["query", "Query params"]]);
    else if (a.type === "awsv4") h += '<div class="grid2">' + f("access_key", "Access key", "{{awsAccessKey}}") + f("secret_key", "Secret key", "{{awsSecretKey}}", "password") +
      f("region", "Region", "us-east-1") + f("service", "Service", "execute-api") + "</div>" + f("session_token", "Session token (optional)");
    else if (a.type === "oauth2") {
      const g = a.grant || "client_credentials";
      h += sel("grant", "Grant type", [["client_credentials", "Client credentials"], ["authorization_code", "Authorization code (sign in in a browser)"], ["password", "Password credentials"]]) +
        '<div class="grid2">' + (g === "authorization_code" ? f("auth_url", "Authorization URL", "https://login.example.com/authorize") : "") + f("token_url", "Access token URL", "https://login.example.com/token") +
        f("client_id", "Client ID", "{{clientId}}") + f("client_secret", "Client secret", "{{clientSecret}}", "password") + f("scope", "Scope", "read write") + f("audience", "Audience (optional)") +
        (g === "password" ? f("username", "Username") + f("password", "Password", "", "password") : "") + "</div>" +
        '<div class="grid2">' + sel("client_auth", "Send client credentials", [["header", "As a Basic auth header"], ["body", "In the request body"]]) +
        sel("add_to", "Add token to", [["header", "Header"], ["query", "Query (access_token)"]]) + "</div>" +
        (g === "authorization_code" ? '<label class="check"><input type="checkbox" data-auth="pkce"' + (a.pkce !== false ? " checked" : "") + "> Use PKCE (recommended)</label>" +
          '<p class="faint tiny">Callback URL to register with the provider: <code>' + esc(location.origin.replace("localhost", "127.0.0.1") + "/oauth/callback") + "</code></p>" : "") +
        '<div class="row tight"><button class="btn small primary" data-oauth="get">Get new token</button><button class="btn small" data-oauth="clear">Clear token</button>' +
        '<span class="faint tiny">Tokens are kept, refreshed and reused automatically.</span></div>';
    }
    return h + "</div>";
  }
  function readAuth(box, old) {
    const a = { type: box.querySelector('[data-auth="type"]').value };
    box.querySelectorAll("[data-auth]").forEach(el => { if (el.dataset.auth !== "type") a[el.dataset.auth] = el.type === "checkbox" ? el.checked : el.value; });
    if (a.type !== (old || {}).type) return Object.assign({ type: a.type }, a.type === "oauth2" ? { grant: "client_credentials", pkce: true } : {});
    return a;
  }
  const SNIPPETS = {
    test: [
      ["Status code is 200", 'pm.test("Status code is 200", function () {\n    pm.response.to.have.status(200);\n});'],
      ["Response time under 500 ms", 'pm.test("Response time is under 500 ms", function () {\n    pm.expect(pm.response.responseTime).to.be.below(500);\n});'],
      ["JSON value check", 'pm.test("Field has the right value", function () {\n    const data = pm.response.json();\n    pm.expect(data.name).to.eql("Ada");\n});'],
      ["Body contains text", 'pm.test("Body contains text", function () {\n    pm.expect(pm.response.text()).to.include("text");\n});'],
      ["Content-Type is JSON", 'pm.test("Content-Type is JSON", function () {\n    pm.response.to.have.header("Content-Type");\n    pm.expect(pm.response.headers.get("Content-Type")).to.include("application/json");\n});'],
      ["Every item has an id", 'pm.test("Every item has an id", function () {\n    pm.response.json().data.forEach(item => pm.expect(item).to.have.property("id"));\n});'],
      ["Save a value for later requests", 'const data = pm.response.json();\npm.environment.set("id", data.id);'],
      ["Save a token from the response", 'pm.environment.set("token", pm.response.json().access_token);'],
      ["Run another request next", 'pm.setNextRequest("Request name");'],
      ["Stop the run here", "pm.setNextRequest(null);"],
    ],
    pre: [
      ["Set a variable", 'pm.variables.set("name", "value");'],
      ["Timestamp variable", 'pm.environment.set("timestamp", Date.now());'],
      ["Unique id per request", 'pm.variables.set("requestId", pm.variables.replaceIn("{{$uuid}}"));'],
      ["Add a header", 'pm.request.headers.add({ key: "X-Request-Id", value: pm.variables.replaceIn("{{$uuid}}") });'],
      ["Add a query parameter", 'pm.request.url.query.add({ key: "page", value: "1" });'],
      ["Log the URL", "console.log(pm.request.url.toString());"],
      ["Skip this request", "pm.execution.skipRequest();"],
    ],
  };
  function scriptsHTML(node, which) {
    const key = which === "pre" ? "pre_script" : "test_script";
    return '<div class="scripts"><div class="tabs sub-tabs">' + [["pre", "Pre-request"], ["test", "Tests"]].map(([k, l]) =>
      '<button class="tab' + (which === k ? " active" : "") + '" data-stab="' + k + '">' + l + ((node[k === "pre" ? "pre_script" : "test_script"] || "").trim() ? ' <i class="dot-on"></i>' : "") + "</button>").join("") +
      '</div><div class="script-wrap"><textarea class="code-area" data-script="' + key + '" spellcheck="false" placeholder="' +
      (which === "pre" ? "// Runs before the request. e.g. pm.environment.set(&quot;timestamp&quot;, Date.now());" : "// Runs after the response. e.g. pm.test(&quot;Status is 200&quot;, () => pm.response.to.have.status(200));") +
      '">' + esc(node[key] || "") + '</textarea><div class="snippets"><div class="faint tiny">SNIPPETS</div>' + SNIPPETS[which].map((s, i) =>
        '<button class="linkish" data-snip="' + which + ":" + i + '">' + esc(s[0]) + "</button>").join("") +
      '<p class="faint tiny">Postman\'s pm.* API. Scripts run in a sandbox in this window' + (A.node ? " (or Node.js)" : "") + ".</p></div></div></div>";
  }

  // ── the editor ───────────────────────────────────────────────────────
  function crumbs(t) {
    const col = colById(t.colId);
    if (!col) return '<span class="faint small">Not saved yet</span>';
    const trail = [col.name].concat(pathTo(col, t.req ? t.req.id : t.nodeId).map(f => f.name)).concat(t.req && !find(col.items, t.req.id) && t.parent ? [(find(col.items, t.parent) || {}).node?.name || ""] : []);
    return '<span class="faint small crumbs">' + trail.filter(Boolean).map(esc).join(" / ") + "</span>";
  }
  function inheritedAuthText(t) {
    const col = colById(t.colId);
    if (!col) return "Not in a collection: no auth is inherited.";
    const chain = [col].concat(pathTo(col, t.req ? t.req.id : t.nodeId));
    if (t.req && !find(col.items, t.req.id) && t.parent) { const pf = find(col.items, t.parent); if (pf) chain.push(...pathTo(col, t.parent), pf.node); }
    for (let i = chain.length - 1; i >= 0; i--) {
      const a = chain[i].auth || {};
      if (a.type && a.type !== "inherit") return "Inherits " + (AUTH_TYPES.find(x => x[0] === a.type) || [, a.type])[1] + " from " + (i === 0 ? "the collection" : "folder " + chain[i].name) + ".";
    }
    return "Inherits no auth.";
  }
  function renderEditor() {
    const t = tab();
    const ed = $("#apiEditor");
    if (!t) { ed.innerHTML = ""; return; }
    if (t.type !== "request") return renderNodeEditor(t);
    const r = t.req;
    const rt = r.kind === "ws" || r.kind === "sse";
    ed.innerHTML = '<div class="req-line"><select id="reqMethod" aria-label="Method" class="m-' + esc(rt ? r.kind.toUpperCase() : r.method) + '">' +
      METHODS.map(m => '<option value="' + m + '"' + (!rt && r.method === m ? " selected" : "") + ">" + m + "</option>").join("") +
      '<option value="WS"' + (r.kind === "ws" ? " selected" : "") + '>WebSocket</option><option value="SSE"' + (r.kind === "sse" ? " selected" : "") + ">SSE</option></select>" +
      '<input id="reqUrl" spellcheck="false" value="' + esc(r.url) + '" placeholder="' + (rt ? (r.kind === "ws" ? "ws://localhost:3000/socket" : "{{baseUrl}}/events") : "{{baseUrl}}/api/users   (or paste a curl command)") + '">' +
      (rt ? '<button class="btn primary" id="rtConnect">' + (t.rt && t.rt.sid ? icon("stop") + "Disconnect" : icon("play") + "Connect") + "</button>"
        : '<button class="btn primary" id="reqSend" title="Send (Ctrl+Enter)">' + icon("play") + "Send</button>") +
      '<button class="btn" id="reqSave" title="Save (Ctrl+S)">Save</button><button class="btn" id="reqSaveAs" title="Save a copy somewhere">Save as</button>' +
      (rt ? "" : '<button class="btn" id="reqCode" title="Code snippet">' + icon("code") + "</button>") + "</div>" +
      '<div class="req-meta"><input id="reqName" spellcheck="false" aria-label="Request name" value="' + esc(r.name) + '">' + crumbs(t) + "</div>" +
      '<div class="tabs req-tabs">' + (rt ? [["params", "Params"], ["headers", "Headers"], ["auth", "Auth"]].concat(r.kind === "ws" ? [["proto", "Settings"]] : []) :
        [["params", "Params"], ["auth", "Auth"], ["headers", "Headers"], ["body", "Body"], ["scripts", "Scripts"], ["checks", "Checks"], ["settings", "Settings"]]
          .concat((r.examples || []).length ? [["examples", "Examples"]] : [])).map(([k, l]) =>
        '<button class="tab' + (t.reqTab === k ? " active" : "") + '" data-rtab="' + k + '">' + l + " <b>" + esc(tabCount(r, k)) + "</b></button>").join("") + "</div>" +
      '<div class="req-pane" id="reqPane"></div><div class="splitter" id="apiSplit" title="Drag to resize"></div><div class="res-box" id="resBox"></div>';
    if (A.paneH) { const p = $("#reqPane"); p.style.maxHeight = p.style.height = A.paneH + "px"; }
    renderPane();
    if (rt) renderRT(); else renderRes();
  }
  function tabCount(r, k) {
    if (k === "params") return (r.params.length + (r.path_vars || []).length) || "";
    if (k === "headers") return r.headers.length || "";
    if (k === "checks") return r.checks.length || "";
    if (k === "examples") return (r.examples || []).length || "";
    if (k === "body") return r.body.mode !== "none" ? ({ json: "JSON", text: "Text", xml: "XML", form: "Form", multipart: "Form-data", binary: "File", graphql: "GraphQL" }[r.body.mode] || "") : "";
    if (k === "auth") return r.auth.type !== "inherit" && r.auth.type !== "none" ? r.auth.type : "";
    if (k === "scripts") return ((r.pre_script || "").trim() ? 1 : 0) + ((r.test_script || "").trim() ? 1 : 0) || "";
    return "";
  }
  function syncPathVars(r) {
    const names = [];
    (r.url.split("?")[0].match(/\/:([A-Za-z_]\w*)/g) || []).forEach(m => names.push(m.slice(2)));
    r.path_vars = names.map(n => (r.path_vars || []).find(p => p.key === n) || { key: n, value: "", enabled: true });
  }
  function renderPane() {
    const t = tab(), r = t.req, pane = $("#reqPane");
    if (!pane) return;
    const k = t.reqTab;
    if (k === "params") {
      pane.innerHTML = '<div class="faint tiny sec">Query parameters</div>' + kvRows(r.params, "params") +
        ((r.path_vars || []).length ? '<div class="faint tiny sec">Path variables (the :name parts of the URL)</div>' + kvRows(r.path_vars, "path_vars", { fixedKeys: true, noAdd: true, noToggle: true }) : "") +
        '<p class="faint tiny">Use {{variables}} anywhere. Path variables come from the URL, e.g. /users/:id.</p>';
    } else if (k === "headers") pane.innerHTML = kvRows(r.headers, "headers", { keyPh: "Header" });
    else if (k === "auth") pane.innerHTML = authHTML(r.auth, true, inheritedAuthText(t));
    else if (k === "body") {
      const b = r.body;
      const modes = [["none", "None"], ["json", "JSON"], ["text", "Text"], ["xml", "XML"], ["form", "x-www-form-urlencoded"], ["multipart", "form-data"], ["binary", "Binary file"], ["graphql", "GraphQL"]];
      let h = '<div class="row tight bodymodes">' + modes.map(([v, l]) => '<label class="check"><input type="radio" name="bmode" value="' + v + '"' + (b.mode === v ? " checked" : "") + "> " + l + "</label>").join("") +
        (b.mode === "json" || b.mode === "graphql" ? '<button class="btn small" data-b="format">Format</button>' : "") + "</div>";
      if (["json", "text", "xml"].includes(b.mode)) h += '<textarea class="code-area" id="bodyText" spellcheck="false">' + esc(b.text || "") + "</textarea>";
      else if (b.mode === "form") h += kvRows(b.form || [], "form");
      else if (b.mode === "multipart") h += kvRows(b.form || [], "form", { files: true });
      else if (b.mode === "binary") h += '<div class="row tight"><button class="btn" data-pickfile="binary">' + icon("paperclip") + (b.file && b.file.name ? esc(b.file.name) : "Choose a file") + "</button>" +
        (b.file && b.file.size ? '<span class="faint small">' + fmtSize(b.file.size) + "</span>" : "") + "</div>";
      else if (b.mode === "graphql") h += '<div class="gql"><label>Query<textarea class="code-area" id="gqlQuery" spellcheck="false" placeholder="query { users { id name } }">' + esc(b.query || "") + "</textarea></label>" +
        '<label>Variables (JSON)<textarea class="code-area" id="gqlVars" spellcheck="false" placeholder="{ &quot;id&quot;: 1 }">' + esc(b.variables || "") + "</textarea></label></div>";
      else h += '<p class="faint small">This request has no body.</p>';
      pane.innerHTML = h;
    } else if (k === "scripts") pane.innerHTML = scriptsHTML(r, t.scriptTab);
    else if (k === "checks") pane.innerHTML = checksHTML(r, t);
    else if (k === "settings") {
      const s = r.settings || {}, g = A.scope.settings || {};
      const tri = (key, label) => '<label>' + label + '<select data-set="' + key + '"><option value=""' + (s[key] === undefined || s[key] === "" ? " selected" : "") + ">Default (" + (g[key] === false ? "off" : "on") + ")</option>" +
        '<option value="on"' + (s[key] === true ? " selected" : "") + '>On</option><option value="off"' + (s[key] === false ? " selected" : "") + ">Off</option></select></label>";
      pane.innerHTML = '<div class="grid2">' + tri("follow", "Follow redirects") + tri("verify", "Check TLS certificates") + tri("cookies", "Send and keep cookies") +
        '<label>Timeout (ms)<input data-set="timeout_ms" inputmode="numeric" placeholder="Default (' + esc(g.timeout_ms || 30000) + ')" value="' + esc(s.timeout_ms || "") + '"></label></div>' +
        '<p class="faint tiny">Defaults, proxy and client certificates are in Settings at the top.</p>';
    } else if (k === "examples") {
      pane.innerHTML = '<div class="examples">' + (r.examples || []).map((x, i) => '<details><summary><span class="pill ' + statusClass(x.status) + '">' + esc(x.status) + "</span> " + esc(x.name) +
        '<button class="icon-btn" data-exdel="' + i + '" title="Delete this example">' + icon("close") + '</button></summary><pre class="res-pre">' + esc(x.body || "(empty body)") + "</pre></details>").join("") +
        '</div><p class="faint tiny">Saved responses. They\'re exported with the collection, so teammates (and Postman) see what this request returns.</p>';
    } else if (k === "proto") {
      pane.innerHTML = '<label>Subprotocols (comma-separated, optional)<input id="rtProto" spellcheck="false" value="' + esc(r.protocols || "") + '" placeholder="graphql-ws"></label>';
    }
  }
  const CHECK_OPS = {
    status: [["eq", "is"], ["ne", "is not"]], time: [["lt", "under (ms)"]], body: [["contains", "contains"], ["not_contains", "doesn't contain"]],
    header: [["exists", "exists"], ["eq", "is"], ["contains", "contains"]], schema: [["valid", "matches"]],
    json: [["exists", "exists"], ["missing", "is absent"], ["eq", "equals"], ["ne", "isn't"], ["contains", "contains"], ["type", "is a (type)"],
      ["length", "has length"], ["gt", ">"], ["lt", "<"]],
  };
  function checksHTML(r, t) {
    return '<div class="checks">' + r.checks.map((c, i) => '<div class="ck" data-ck="' + i + '"><input type="checkbox" data-k="enabled"' + (c.enabled !== false ? " checked" : "") + ">" +
      '<select data-k="kind">' + [["status", "Status"], ["json", "JSON field"], ["header", "Header"], ["body", "Body text"], ["time", "Time"], ["schema", "JSON schema"]].map(([v, l]) =>
        '<option value="' + v + '"' + (c.kind === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select>" +
      (c.kind === "schema" ? '<button class="btn small" data-editschema="' + i + '">Edit schema</button><span class="faint tiny">' + esc(Object.keys((c.schema || {}).properties || {}).slice(0, 4).join(", ") || "any JSON") + "</span><span></span>"
        : (c.kind === "json" || c.kind === "header" ? '<input data-k="path" placeholder="' + (c.kind === "json" ? "data[0].id" : "Content-Type") + '" value="' + esc(c.path || "") + '" spellcheck="false">' : "<span></span>") +
          '<select data-k="op">' + (CHECK_OPS[c.kind] || []).map(([v, l]) => '<option value="' + v + '"' + (c.op === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select>" +
          (["exists", "missing"].includes(c.op) ? "<span></span>" : '<input data-k="value" placeholder="' + ({ status: "200 or 2xx", time: "500" }[c.kind] || (c.op === "type" ? "string" : "value")) +
            '" value="' + esc(c.value ?? "") + '" spellcheck="false">')) +
      '<button class="icon-btn" data-ckdel="' + i + '" title="Remove">' + icon("close") + "</button></div>").join("") + "</div>" +
      '<div class="row tight"><button class="btn small" data-b="addcheck">' + icon("plus") + "Add check</button>" +
      (t.res && t.res.json != null ? '<button class="btn small" data-b="suggestchecks">' + icon("spark") + "Checks from this response</button>" : "") + "</div>" +
      '<p class="faint tiny">No-code checks run with every send and every collection run, next to any test scripts. JSON paths look like <code>data.items[0].id</code>.</p>';
  }

  // ── response ─────────────────────────────────────────────────────────
  function statusClass(s) { return !s ? "bad" : s < 300 ? "ok" : s < 400 ? "warn" : "bad"; }
  function fmtSize(n) { return n == null ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
  function highlightJSON(text) {
    return esc(text).replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      (m, str, colon, kw, num) => str ? '<span class="' + (colon ? "jk" : "js") + '">' + str + "</span>" + (colon || "") : kw ? '<span class="jb">' + kw + "</span>" : '<span class="jn">' + num + "</span>");
  }
  function markSearch(html, term) {
    if (!term) return html;
    const re = new RegExp("(" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") + ")", "gi");
    return html.replace(/(<[^>]*>)|([^<]+)/g, (m, tag, text) => tag || text.replace(re, "<mark>$1</mark>"));
  }
  function renderRes() {
    const t = tab(), res = t.res, box = $("#resBox");
    if (!box) return;
    if (!res) { box.innerHTML = '<div class="res-empty faint">Send the request to see the response. Ctrl+Enter sends, Ctrl+S saves.</div>'; return; }
    if (res.pending) { box.innerHTML = '<div class="res-empty"><span class="spinner"></span> Sending…</div>'; return; }
    const checks = res.checks || [], tests = res.tests || [], cons = res.console || [], errs = res.script_errors || [];
    const allTests = checks.map(c => c.pass).concat(tests.map(x => x.passed));
    const passed = allTests.filter(Boolean).length;
    let h = '<div class="res-head">';
    if (res.error) h += '<span class="pill bad">' + (res.needs_token ? "Sign in needed" : "Error") + '</span><span class="res-err">' + esc(res.error) + "</span>";
    else if (res.skipped) h += '<span class="pill warn">Skipped by a pre-request script</span>';
    else h += '<span class="pill ' + statusClass(res.status) + '">' + esc(res.status + " " + (res.reason || "")) + '</span><span class="faint small">' + esc(res.ms + " ms · " + fmtSize(res.size)) + "</span>";
    if (allTests.length) h += '<span class="pill ' + (passed === allTests.length ? "ok" : "bad") + '">' + icon(passed === allTests.length ? "check" : "cross") + passed + "/" + allTests.length + " passed</span>";
    if (errs.length) h += '<span class="pill bad" title="' + esc(errs.join("\n")) + '">Script error</span>';
    if (res.missing && res.missing.length) h += '<span class="pill warn" title="Set these in an environment or collection">Unknown: ' + esc(res.missing.map(m => "{{" + m + "}}").join(" ")) + "</span>";
    h += '<span class="res-acts"><button class="btn small" data-r="ai">' + icon("spark") + "Ask AI</button>" +
      (res.status ? '<button class="btn small" data-r="example" title="Save this response as an example">Save example</button><button class="btn small" data-r="file" title="Save the body to a file">' + icon("file") + "</button>" : "") + "</span></div>";
    if (!res.error && !res.skipped) {
      const tabs = [["body", "Body"], ["headers", "Headers (" + (res.headers || []).length + ")"], ["cookies", "Cookies (" + (res.cookies || []).length + ")"],
        ["tests", "Tests (" + passed + "/" + allTests.length + ")"], ["console", "Console" + (cons.length || errs.length ? " (" + (cons.length + errs.length) + ")" : "")], ["sent", "Request"]];
      h += '<div class="tabs res-tabs">' + tabs.map(([k, l]) => '<button class="tab' + (t.resTab === k ? " active" : "") + '" data-rt="' + k + '">' + l + "</button>").join("") +
        (t.resTab === "body" && res.body ? '<span class="res-tools"><input id="resFind" type="search" placeholder="Find in body" value="' + esc(t.find || "") + '">' +
          (res.json != null ? '<label class="check tiny"><input type="checkbox" id="resRaw"' + (t.raw ? " checked" : "") + "> raw</label>" : "") + "</span>" : "") + "</div>";
      let body = "";
      if (t.resTab === "body") {
        if (res.body_b64 && /^image\//.test(res.content_type || "")) body = '<img class="res-img" src="data:' + esc(res.content_type) + ";base64," + res.body_b64 + '" alt="">';
        else if (res.body_b64) body = '<p class="faint small pad">Binary response (' + esc(res.content_type || "data") + ", " + fmtSize(res.size) + "). Save it to a file to open it.</p>";
        else if (res.json != null && !t.raw) body = '<pre class="res-pre json">' + markSearch(highlightJSON(JSON.stringify(res.json, null, 2)), t.find) + "</pre>";
        else body = '<pre class="res-pre">' + markSearch(esc(res.body || "(empty body)"), t.find) + "</pre>";
        if (res.truncated) body += '<p class="faint tiny pad">Only the first 5 MB is shown.</p>';
      } else if (t.resTab === "headers") {
        body = '<table class="table hdrs">' + (res.headers || []).map(([k, v]) => "<tr><td>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("") + "</table>";
      } else if (t.resTab === "cookies") {
        body = (res.cookies || []).length ? '<table class="table hdrs">' + res.cookies.map(c => "<tr><td>" + esc(c.name) + "</td><td>" + esc(c.value) + "</td></tr>").join("") + "</table>"
          : '<p class="faint small pad">The response set no cookies. Stored cookies are under Cookies at the top.</p>';
      } else if (t.resTab === "tests") {
        body = allTests.length ? '<div class="ckres">' + tests.map(x => '<div class="' + (x.passed ? "ok" : "bad") + '">' + icon(x.passed ? "check" : "cross") + "<span>" + esc(x.name) +
          "</span>" + (x.passed ? "" : '<span class="faint">' + esc(x.error) + "</span>") + (x.where && x.where !== "request" ? '<span class="tag">' + esc(x.where) + "</span>" : "") + "</div>").join("") +
          checks.map(c => '<div class="' + (c.pass ? "ok" : "bad") + '">' + icon(c.pass ? "check" : "cross") + "<span>" + esc(c.label) + '</span><span class="faint">got ' + esc(c.actual) + '</span><span class="tag">check</span></div>').join("") + "</div>"
          : '<p class="faint small pad">No tests. Add test scripts on the Scripts tab (there are snippets), or no-code checks on the Checks tab.</p>';
      } else if (t.resTab === "console") {
        body = cons.length || errs.length ? '<div class="console">' + errs.map(e => '<div class="error">' + esc(e) + "</div>").join("") +
          cons.map(c => '<div class="' + esc(c.level) + '">' + esc(c.text) + "</div>").join("") + "</div>" : '<p class="faint small pad">console.log output from scripts shows here.</p>';
      } else {
        body = '<pre class="res-pre">' + esc(res.method + " " + res.url + "\n" + (res.sent_headers || []).map(([k, v]) => k + ": " + v).join("\n")) + "</pre>";
      }
      h += '<div class="res-body">' + body + "</div>";
    }
    box.innerHTML = h;
  }

  // ── WebSocket / SSE ──────────────────────────────────────────────────
  function renderRT() {
    const t = tab(), box = $("#resBox");
    if (!box) return;
    t.rt = t.rt || { sid: "", log: [] };
    const ws = t.req.kind === "ws";
    box.innerHTML = '<div class="res-head"><span class="pill ' + (t.rt.sid ? "ok" : "warn") + '">' + (t.rt.sid ? "Connected" : "Not connected") + "</span>" +
      (t.rt.status ? '<span class="faint small">' + esc(t.rt.status) + "</span>" : "") + '<span class="res-acts"><button class="btn small" data-rtclear="1">' + icon("clear") + "Clear</button></span></div>" +
      '<div class="rt-log" id="rtLog">' + t.rt.log.map(rtLine).join("") + (t.rt.log.length ? "" : '<p class="faint small pad">' + (ws ? "Connect, then send messages below. Incoming messages appear here." : "Connect to start receiving events.") + "</p>") + "</div>" +
      (ws ? '<div class="rt-send"><textarea id="rtMsg" class="code-area" rows="3" spellcheck="false" placeholder="Message (Ctrl+Enter sends)">' + esc(t.rt.draft || "") + '</textarea><button class="btn primary" id="rtSend"' + (t.rt.sid ? "" : " disabled") + ">Send</button></div>" : "");
    const l = $("#rtLog");
    l.scrollTop = l.scrollHeight;
  }
  function rtLine(m) {
    const when = new Date(m.ts * 1000).toLocaleTimeString();
    let data = m.data;
    if (m.kind === "binary") data = "(binary, base64) " + data;
    else { try { if (/^[[{]/.test(data.trim())) data = JSON.stringify(JSON.parse(data), null, 2); } catch (_) { /* not JSON */ } }
    return '<div class="rtm ' + esc(m.dir) + '"><span class="rtd">' + icon(m.dir === "in" ? "chevron-down" : m.dir === "out" ? "chevron-up" : "dot") + "</span>" +
      (m.event && m.event !== "message" ? '<span class="tag">' + esc(m.event) + "</span>" : "") + '<pre>' + esc(data) + '</pre><span class="faint tiny">' + esc(when) + "</span></div>";
  }

  // ── the collection / folder editor ───────────────────────────────────
  function renderNodeEditor(t) {
    const n = t.node, col = colById(t.colId);
    const isCol = t.type === "collection";
    const subs = isCol ? [["overview", "Overview"], ["auth", "Auth"], ["variables", "Variables"], ["scripts", "Scripts"]] : [["auth", "Auth"], ["scripts", "Scripts"], ["overview", "Description"]];
    let pane = "";
    if (t.sub === "auth") pane = authHTML(n.auth, !isCol, "Uses the collection's auth.") + '<p class="faint tiny">Requests inside set to "Inherit from parent" use this.</p>';
    else if (t.sub === "variables") pane = '<p class="faint small">Collection variables: shared by every request in it, saved with the collection (and in Git, if it\'s shared). Environment values override them. Use the environment for secrets.</p>' + kvRows(n.variables, "variables");
    else if (t.sub === "scripts") pane = '<p class="faint small">These run for every request in this ' + (isCol ? "collection" : "folder") + ": pre-request scripts before the request's own, tests after.</p>" + scriptsHTML(n, t.scriptTab);
    else {
      const reqs = isCol ? allRequests(col) : allRequests(find(col.items, n.id).node);
      pane = '<label>Description<textarea id="nodeDesc" rows="4" placeholder="What this ' + (isCol ? "collection" : "folder") + ' is for (Markdown)">' + esc(n.description || "") + "</textarea></label>" +
        '<p class="small">' + reqs.length + " request" + (reqs.length === 1 ? "" : "s") + ".</p>" +
        '<div class="row tight"><button class="btn primary" data-n="run">' + icon("play") + "Run " + (isCol ? "collection" : "folder") + "</button>" +
        (isCol ? '<button class="btn" data-n="export">Export (Postman v2.1)</button>' +
          (A.project ? (col.location === "project" ? '<button class="btn" data-n="unshare">Keep it in the app only</button>' : '<button class="btn" data-n="share">Share with the team (store in the project, for Git)</button>') : "") : "") + "</div>" +
        (isCol && col.location === "project" ? '<p class="faint small">Stored in <code>' + esc(col.file) + "</code> in the project, in Postman format: commit it and teammates (and Postman) can open it. Environments and secrets stay on this computer.</p>" : "");
    }
    $("#apiEditor").innerHTML = '<div class="node-head">' + icon(isCol ? "book" : "folder") + '<input id="nodeName" value="' + esc(n.name) + '" spellcheck="false">' +
      '<button class="btn primary" id="nodeSave">Save</button></div>' + '<div class="tabs req-tabs">' + subs.map(([k, l]) => '<button class="tab' + (t.sub === k ? " active" : "") + '" data-nsub="' + k + '">' + l + "</button>").join("") + "</div>" +
      '<div class="req-pane node-pane" id="reqPane">' + pane + "</div>";
  }
  async function saveNode(t) {
    const col = colById(t.colId);
    const n = t.node;
    if (t.type === "collection") await putCollection(col, { name: n.name, auth: n.auth, variables: n.variables, pre_script: n.pre_script, test_script: n.test_script, description: n.description });
    else {
      const f = find(col.items, n.id);
      Object.assign(f.node, { name: n.name, auth: n.auth, pre_script: n.pre_script, test_script: n.test_script, description: n.description });
      await putCollection(col);
    }
    t.dirty = false;
    renderTabs(); renderSide();
    toast("Saved " + n.name);
  }

  // ── sending and saving ───────────────────────────────────────────────
  async function send() {
    const t = tab();
    if (!t || t.type !== "request") return;
    if (t.req.kind) return toggleRT();
    t.res = { pending: true };
    renderRes();
    try {
      t.res = await api("POST", "http/send", { project: A.project, request: t.req, collection: t.colId, parent: t.parent });
      const failing = (t.res.checks || []).some(c => !c.pass) || (t.res.tests || []).some(x => !x.passed);
      if (failing && t.resTab === "body") t.resTab = "tests";
      if ((t.res.script_errors || []).length && t.resTab === "body") t.resTab = "console";
    } catch (e) { t.res = { error: e.message }; }
    if (tab() === t) { renderRes(); if (t.reqTab === "checks") renderPane(); }
    await load();
    if (A.sideTab === "history") renderSide();
  }
  async function save(asNew) {
    const t = tab();
    if (t.type !== "request") return saveNode(t);
    let col = colById(t.colId);
    if (!col || asNew) {
      const dest = await pickPlace(asNew ? "Save a copy" : "Save request");
      if (!dest) return;
      col = dest.col;
      if (asNew) t.req = Object.assign(clone(t.req), { id: uid(), name: dest.name || t.req.name + " copy" });
      else if (dest.name) t.req.name = dest.name;
      t.colId = col.id;
      t.parent = dest.parent;
    }
    const f = find(col.items, t.req.id);
    if (f) f.list[f.index] = clone(t.req);
    else {
      const parent = t.parent ? find(col.items, t.parent) : null;
      (parent && parent.node.folder ? parent.node.items : col.items).push(clone(t.req));
      if (parent) A.open.add(parent.node.id);
    }
    await putCollection(col);
    t.dirty = false;
    t.saved = true;
    A.open.add(col.id);
    renderTabs(); renderEditor(); renderSide();
    toast("Saved to " + col.name);
  }
  function pickPlace(title) {
    return new Promise(resolve => {
      const list = cols();
      const opts = [];
      list.forEach(c => {
        opts.push([c.id, "", c.name]);
        const walk = (items, depth) => (items || []).forEach(n => { if (n.folder) { opts.push([c.id, n.id, "\u2003".repeat(depth) + n.name]); walk(n.items, depth + 1); } });
        walk(c.items, 1);
      });
      const d = openModal("<h3>" + esc(title) + '</h3><label>Name<input id="ppName" value="' + esc(tab().req.name) + '"></label>' +
        '<label>Collection or folder<select id="ppWhere">' + opts.map(([c, f, l], i) => '<option value="' + i + '">' + esc(l) + "</option>").join("") +
        '<option value="new">+ A new collection</option></select></label><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Save</button></div>');
      d.querySelector('[data-a="cancel"]').onclick = () => { closeModal(); resolve(null); };
      d.querySelector('[data-a="ok"]').onclick = guard(async () => {
        const v = $("#ppWhere").value, name = $("#ppName").value.trim();
        closeModal();
        if (v === "new") {
          const cname = prompt("Name for the new collection:", A.project ? "API requests" : "My requests");
          if (!cname) return resolve(null);
          const c = (await api("POST", "http/collections", { project: A.project, name: cname })).collection;
          A.scope.collections.push(c);
          return resolve({ col: c, parent: "", name });
        }
        const [cid, fid] = opts[+v];
        resolve({ col: colById(cid), parent: fid, name });
      });
    });
  }
  async function toggleRT() {
    const t = tab();
    t.rt = t.rt || { sid: "", log: [] };
    if (t.rt.sid) {
      await api("POST", "http/rt", { action: "close", id: t.rt.sid });
      t.rt.sid = "";
      renderEditor();
      return;
    }
    const r = await api("POST", "http/rt", { action: "open", project: A.project, request: t.req, collection: t.colId, parent: t.parent });
    t.rt.sid = r.id;
    t.rt.status = r.status;
    t.rt.log.push({ dir: "sys", data: "Connected: " + r.status, ts: Date.now() / 1000 });
    // messages that arrived before this reply (a server greeting) were held back
    const early = A.rtEarly[r.id] || [];
    delete A.rtEarly[r.id];
    early.forEach(ev => (ev.type === "rt" ? t.rt.log.push(ev) : (t.rt.sid = "")));
    renderEditor();
  }

  // ── tree actions ─────────────────────────────────────────────────────
  async function nodeMenu(col, nodeId) {
    const isCol = col.id === nodeId;
    const f = isCol ? null : find(col.items, nodeId);
    const n = isCol ? col : f.node;
    const folderLike = isCol || n.folder;
    const d = openModal("<h3>" + esc(n.name) + '</h3><div class="menu-list">' +
      (folderLike ? '<button data-m="settings">' + icon("edit") + (isCol ? "Collection settings (auth, variables, scripts)" : "Folder settings (auth, scripts)") + "</button>" +
        '<button data-m="addreq">' + icon("plus") + "Add request</button>" + '<button data-m="addfolder">' + icon("folder-plus") + "Add folder</button>" + '<button data-m="run">' + icon("play") + "Run</button>" : "") +
      '<button data-m="rename">' + icon("edit") + "Rename</button>" + (isCol ? "" : '<button data-m="dup">' + icon("copy") + "Duplicate</button>") +
      (isCol ? '<button data-m="export">' + icon("file") + "Export (Postman v2.1)</button>" : "") +
      (isCol && A.project ? '<button data-m="' + (col.location === "project" ? "unshare" : "share") + '">' + icon("branch") + (col.location === "project" ? "Keep it in the app only" : "Share with the team through Git") + "</button>" : "") +
      '<button data-m="delete" class="danger">' + icon("trash") + "Delete</button></div>");
    d.querySelectorAll("[data-m]").forEach(b => (b.onclick = guard(async () => {
      const m = b.dataset.m;
      closeModal();
      if (m === "settings") openNode(col.id, nodeId);
      else if (m === "addreq") { A.open.add(nodeId); A.open.add(col.id); const t = newTab(null, col.id, isCol ? "" : nodeId); t.saved = false; await save(); }
      else if (m === "addfolder") {
        const name = prompt("Folder name:", "New folder");
        if (!name) return;
        (isCol ? col.items : n.items).push({ id: uid(), name, folder: true, items: [], auth: { type: "inherit" }, pre_script: "", test_script: "" });
        A.open.add(nodeId);
        await putCollection(col);
        renderSide();
      } else if (m === "run") runDialog(col, isCol ? null : n);
      else if (m === "rename") {
        const name = prompt("New name:", n.name);
        if (!name) return;
        if (isCol) await putCollection(col, { name }); else { n.name = name; await putCollection(col); }
        A.tabs.forEach(t => { if (t.req && t.req.id === nodeId) t.req.name = name; if (t.nodeId === nodeId) t.node.name = name; });
        renderAll();
      } else if (m === "dup") {
        const copy = clone(n);
        const reid = x => { x.id = uid(); (x.items || []).forEach(reid); };
        reid(copy);
        copy.name += " copy";
        f.list.splice(f.index + 1, 0, copy);
        await putCollection(col);
        renderSide();
      } else if (m === "export") app.download("http/collections/" + col.id + "/export");
      else if (m === "share" || m === "unshare") {
        const r = await api("POST", "http/collections/" + col.id + "/move", { project: A.project, to: m === "share" ? "project" : "app" });
        Object.assign(col, r.collection);
        toast(m === "share" ? "Stored in " + col.file + ". Commit it to share it with your team." : "Moved back into the app.");
        await load(); renderAll();
      } else if (m === "delete") {
        const count = isCol ? allRequests(col).length : n.folder ? allRequests(n).length : 0;
        if (!confirm("Delete " + n.name + (count ? " and its " + count + " request" + (count === 1 ? "" : "s") : "") + "?")) return;
        if (isCol) await api("DELETE", "http/collections/" + col.id + "?" + q());
        else { f.list.splice(f.index, 1); await putCollection(col); }
        A.tabs = A.tabs.filter(t => !(t.colId === col.id && (isCol || t.nodeId === nodeId || (t.req && t.req.id === nodeId))));
        if (!A.tabs.length) newTab();
        A.active = Math.min(A.active, A.tabs.length - 1);
        await load(); renderAll();
      }
    })));
  }
  async function moveNode(srcCol, nodeId, dstCol, targetId, where) {
    if (nodeId === targetId) return;
    const f = find(srcCol.items, nodeId);
    if (!f) return;
    if (f.node.folder && targetId && find(f.node.items, targetId)) return toast("A folder can't go inside itself.", true);
    f.list.splice(f.index, 1);
    let list = dstCol.items, idx = list.length;
    if (targetId && targetId !== dstCol.id) {
      const t = find(dstCol.items, targetId);
      if (t && where === "into" && t.node.folder) { list = t.node.items; idx = list.length; A.open.add(t.node.id); }
      else if (t) { list = t.list; idx = t.index; }
    }
    list.splice(idx, 0, f.node);
    await putCollection(srcCol);
    if (dstCol !== srcCol) await putCollection(dstCol);
    A.tabs.forEach(t => { if (t.req && t.req.id === nodeId) t.colId = dstCol.id; });
    renderSide();
  }

  // ── runner ───────────────────────────────────────────────────────────
  function runDialog(col, folder) {
    const n = folder || col;
    const reqs = allRequests(n);
    let dataText = "", dataName = "";
    const d = openModal("<h3>Run " + esc(n.name) + '</h3><p class="faint small">' + reqs.length + " request" + (reqs.length === 1 ? "" : "s") + ", in order, with their scripts and checks." +
      ($("#apiEnv").value ? " Environment: <b>" + esc($("#apiEnv").selectedOptions[0].textContent) + "</b>." : " No environment selected.") + "</p>" +
      '<div class="grid2"><label>Iterations<input id="runIter" inputmode="numeric" value="1"></label><label>Delay between requests (ms)<input id="runDelay" inputmode="numeric" value="0"></label></div>' +
      '<label>Data file (CSV or JSON): one iteration per row; its columns become {{variables}}<input type="file" id="runData" accept=".csv,.json,text/csv,application/json"></label><p class="faint tiny" id="runDataInfo"></p>' +
      '<label class="check"><input type="checkbox" id="runBail"> Stop at the first failure</label>' +
      '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="run">' + icon("play") + "Run</button></div>");
    $("#runData").onchange = async e => {
      const f = e.target.files[0];
      if (!f) return;
      dataText = await f.text();
      dataName = f.name;
      const rows = f.name.toLowerCase().endsWith(".json") ? (() => { try { const j = JSON.parse(dataText); return Array.isArray(j) ? j.length : 1; } catch (_) { return "?"; } })() : Math.max(0, dataText.trim().split(/\r?\n/).length - 1);
      $("#runDataInfo").textContent = rows + " row" + (rows === 1 ? "" : "s") + " → " + rows + " iteration" + (rows === 1 ? "" : "s");
    };
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="run"]').onclick = guard(async () => {
      const runId = uid();
      const body = { project: A.project, folder: folder ? folder.id : "", iterations: +$("#runIter").value || 1, delay: +$("#runDelay").value || 0,
        bail: $("#runBail").checked, data: dataText, data_name: dataName, run: runId };
      A.runs[runId] = [];
      d.innerHTML = "<h3>Running " + esc(n.name) + '…</h3><div class="runres" id="runLive"></div><div class="res-empty"><span class="spinner"></span> Working…</div>';
      d.classList.add("wide");
      let rep;
      try { rep = await api("POST", "http/collections/" + col.id + "/run", body); }
      catch (e) { closeModal(); throw e; }
      finally { delete A.runs[runId]; }
      showReport(col, n, rep);
      await load();
    });
  }
  function runRow(x) {
    const res = x.response || {};
    const bad = (x.checks || []).filter(c => !c.pass).map(c => esc(c.label) + ": got " + esc(c.actual)).concat((x.tests || []).filter(t => !t.passed).map(t => esc(t.name) + ": " + esc(t.error)))
      .concat((x.script_errors || []).map(esc));
    return '<div class="' + (x.skipped ? "skip" : x.pass ? "ok" : "bad") + '">' + icon(x.skipped ? "dot" : x.pass ? "check" : "cross") + '<b class="m m-' + esc(res.method || "GET") + '">' + esc(mshort(res.method || "")) +
      "</b><span>" + esc(x.name) + (x.iteration ? ' <span class="faint">#' + (x.iteration + 1) + "</span>" : "") + '</span><span class="faint small">' + esc(res.error ? res.error : x.skipped ? "skipped" : res.status + " · " + res.ms + " ms") + "</span>" +
      ((x.tests || []).length ? '<span class="faint tiny">' + x.tests.filter(t => t.passed).length + "/" + x.tests.length + " tests</span>" : "") +
      (bad.length ? '<div class="why">' + bad.join("<br>") + "</div>" : "") + "</div>";
  }
  function showReport(col, n, rep) {
    const ok = rep.failed === 0;
    const d = openModal("<h3>" + esc(n.name) + ': <span class="' + (ok ? "okc" : "badc") + '">' + rep.passed + " of " + (rep.requests - rep.skipped) + " passed</span></h3>" +
      '<p class="faint small">' + rep.tests + " checks and tests, " + rep.failed_tests + " failed · " + rep.iterations + " iteration" + (rep.iterations === 1 ? "" : "s") + " · " + rep.seconds + "s" +
      (rep.stopped ? " · stopped early" : "") + (A.project ? " · recorded on the project's Tests tab" : "") + "</p>" +
      '<div class="runres">' + rep.results.map(runRow).join("") + "</div>" +
      '<div class="row between"><span><button class="btn small" data-f="html">HTML report</button> <button class="btn small" data-f="junit">JUnit XML</button> <button class="btn small" data-f="json">JSON</button></span><span>' +
      (ok ? "" : '<button class="btn" data-a="ai">' + icon("spark") + "Ask the AI to fix the failures</button> ") + '<button class="btn primary" data-a="close">Close</button></span></div>');
    d.classList.add("wide");
    d.querySelector('[data-a="close"]').onclick = closeModal;
    d.querySelectorAll("[data-f]").forEach(b => (b.onclick = () => app.download("http/reports/" + rep.report + "?format=" + b.dataset.f)));
    const ai = d.querySelector('[data-a="ai"]');
    if (ai) ai.onclick = guard(async () => {
      closeModal();
      const fails = rep.results.filter(x => !x.pass && !x.skipped).slice(0, 15).map(x => "- " + x.name + " (" + (x.response.method || "") + " " + (x.response.url || "") + "): " +
        (x.response.error || (x.checks || []).filter(c => !c.pass).map(c => c.label + ", got " + c.actual).concat((x.tests || []).filter(t => !t.passed).map(t => t.name + ": " + t.error)).concat(x.script_errors || []).join("; "))).join("\n");
      await askAI("Running the API " + (n === col ? "collection" : "folder") + " \"" + n.name + "\" failed for these requests:\n" + fails +
        "\n\nFind out why (look at the endpoint code and the server logs) and fix it, then run the collection again with run_api_collection.");
    });
  }

  // ── dialogs ──────────────────────────────────────────────────────────
  function envDialog(selectId) {
    const envs = A.scope.environments;
    let cur = envs.find(e => e.id === (selectId || $("#apiEnv").value)) || envs[0] || null;
    let globals = clone(A.scope.globals || []);
    let showGlobals = false;
    const draw = () => {
      const d = openModal("<h3>Variables</h3><p class=\"faint small\">Use {{name}} anywhere in requests. Priority: a script's pm.variables, then data file values, then the <b>environment</b>, then collection variables, then <b>globals</b>. " +
        "Values marked <b>secret</b> are hidden and never shown to the AI.</p>" +
        '<div class="row tight">' + envs.map(e => '<button class="btn small' + (!showGlobals && cur && e.id === cur.id ? " on" : "") + '" data-env="' + esc(e.id) + '">' + esc(e.name) + "</button>").join("") +
        '<button class="btn small" data-a="new">' + icon("plus") + "New environment</button><button class=\"btn small" + (showGlobals ? " on" : "") + '" data-a="globals">Globals</button></div>' +
        (showGlobals ? '<p class="faint tiny">Globals apply to every collection in this ' + (A.project ? "project" : "space") + ".</p>" + kvRows(globals, "globals", { noToggle: true }) +
          '<div class="row end"><button class="btn" data-a="cancel">Close</button> <button class="btn primary" data-a="saveglobals">Save globals</button></div>'
          : cur ? '<label>Name<input id="envName" value="' + esc(cur.name) + '"></label>' + kvRows(cur.vars, "env", { secretCol: true, keyPh: "name, e.g. baseUrl" }) +
          '<div class="row between"><span><button class="btn danger" data-a="delete">Delete</button> <button class="btn" data-a="export">Export</button></span><span><button class="btn" data-a="cancel">Close</button> <button class="btn primary" data-a="save">Save</button></span></div>'
            : '<p class="faint small">No environments yet. Press New environment: it starts with {{baseUrl}}.</p><div class="row end"><button class="btn" data-a="cancel">Close</button></div>'));
      d.classList.add("wide");
      const collect = () => {
        if (showGlobals) { const kv = d.querySelector('.kv[data-kv="globals"]'); if (kv) globals = readKV(kv); return; }
        if (!cur) return;
        cur.name = $("#envName").value;
        cur.vars = readKV(d.querySelector('.kv[data-kv="env"]')).map(v => ({ key: v.key.trim(), value: v.value, secret: !!v.secret, enabled: v.enabled })).filter(v => v.key);
      };
      d.addEventListener("input", e => {
        const row = e.target.closest(".kvr.new");
        if (row && e.target.dataset.f === "key") { collect(); draw(); const keys = $$(".dialog .kvr:not(.new) [data-f=key]"); const el = keys[keys.length - 1]; if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
      });
      d.addEventListener("change", e => { if (e.target.dataset.f === "secret") e.target.closest(".kvr").querySelector('[data-f="value"]').type = e.target.checked ? "password" : "text"; });
      d.querySelectorAll("[data-kvdel]").forEach(b => (b.onclick = () => { collect(); (showGlobals ? globals : cur.vars).splice(+b.dataset.kvdel, 1); draw(); }));
      d.querySelector('[data-a="cancel"]').onclick = () => { closeModal(); renderEnv(); };
      d.querySelectorAll("[data-env]").forEach(b => (b.onclick = () => { collect(); showGlobals = false; cur = envs.find(e => e.id === b.dataset.env); draw(); }));
      d.querySelector('[data-a="globals"]').onclick = () => { collect(); showGlobals = true; draw(); };
      d.querySelector('[data-a="new"]').onclick = guard(async () => {
        collect();
        const name = prompt("Name for the environment (e.g. Local, Staging):", envs.length ? "Staging" : "Local");
        if (!name) return;
        const e = (await api("POST", "http/environments", { project: A.project, name })).environment;
        envs.push(e);
        A.scope.active_env = e.id;
        cur = e;
        showGlobals = false;
        draw();
      });
      const sg = d.querySelector('[data-a="saveglobals"]');
      if (sg) sg.onclick = guard(async () => { collect(); await api("PUT", "http/globals", { project: A.project, vars: globals }); A.scope.globals = globals; toast("Globals saved"); });
      const save = d.querySelector('[data-a="save"]');
      if (save) save.onclick = guard(async () => { collect(); await api("PUT", "http/environments/" + cur.id, { name: cur.name, vars: cur.vars }); toast("Environment saved"); closeModal(); await load(); renderEnv(); });
      const ex = d.querySelector('[data-a="export"]');
      if (ex) ex.onclick = () => { app.download("http/environments/" + cur.id + "/export"); toast("Exported in Postman format. Secret values are left empty."); };
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
    const list = cols();
    const d = openModal("<h3>Import</h3><p class=\"faint small\">Paste or choose: a <b>curl</b> command (e.g. Copy as cURL in your browser's dev tools), a <b>Postman</b> collection or environment, or an <b>OpenAPI / Swagger</b> spec (JSON or YAML). " +
      "An OpenAPI spec becomes a collection with folders, example bodies and schema checks.</p>" +
      '<textarea id="impText" rows="9" spellcheck="false" class="code-area" placeholder="curl -X POST https://… -H \'Content-Type: application/json\' -d \'{…}\'"></textarea>' +
      '<div class="row tight"><input type="file" id="impFile" accept=".json,.yaml,.yml,application/json"></div>' +
      (list.length ? '<label>Put a curl request in<select id="impCol"><option value="">A new "Imported" collection</option>' + list.map(c => '<option value="' + esc(c.id) + '">' + esc(c.name) + "</option>").join("") + "</select></label>" : "") +
      '<p class="small" id="impMsg"></p><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Import</button></div>');
    d.classList.add("wide");
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    $("#impFile").onchange = async e => { const f = e.target.files[0]; if (f) $("#impText").value = await f.text(); };
    d.querySelector('[data-a="ok"]').onclick = guard(async () => {
      try {
        const r = await api("POST", "http/import", { project: A.project, text: $("#impText").value, collection: ($("#impCol") || {}).value || "" });
        closeModal();
        await load();
        if (r.environment && !r.collection) { renderAll(); toast("Imported the environment " + r.environment.name); return; }
        A.open.add(r.collection.id);
        A.sideTab = "collections";
        if (r.request) { newTab(r.request, r.collection.id, ""); tab().dirty = false; tab().saved = true; }
        renderAll();
        toast(r.request ? "Imported " + r.request.name : "Imported " + r.collection.name + " (" + allRequests(r.collection).length + " requests)" + (r.environment ? " and an environment with its variables" : ""));
      } catch (e) { $("#impMsg").textContent = e.message; $("#impMsg").style.color = "var(--red)"; }
    });
  }
  async function codeDialog() {
    const t = tab();
    let lang = store("va-code-lang") || "curl";
    const langs = [["curl", "curl"], ["python", "Python"], ["javascript", "JavaScript"], ["java", "Java"], ["csharp", "C#"], ["go", "Go"], ["powershell", "PowerShell"]];
    const draw = async () => {
      const r = await api("POST", "http/code", { project: A.project, request: t.req, collection: t.colId, parent: t.parent, lang });
      const d = openModal("<h3>Code</h3><div class=\"tabs\">" + langs.map(([k, l]) => '<button class="tab' + (k === lang ? " active" : "") + '" data-lang="' + k + '">' + l + "</button>").join("") +
        '</div><pre class="cmd code-out">' + esc(r.code) + '</pre><p class="faint tiny">Variables are filled in with the current environment, including secrets: be careful where you paste it.</p>' +
        '<div class="row end"><button class="btn" data-a="close">Close</button><button class="btn primary" data-a="copy">' + icon("copy") + "Copy</button></div>");
      d.classList.add("wide");
      d.querySelectorAll("[data-lang]").forEach(b => (b.onclick = guard(async () => { lang = b.dataset.lang; store("va-code-lang", lang); await draw(); })));
      d.querySelector('[data-a="close"]').onclick = closeModal;
      d.querySelector('[data-a="copy"]').onclick = guard(async () => { await navigator.clipboard.writeText(r.code); toast("Copied"); });
    };
    await draw();
  }
  async function cookiesDialog() {
    const r = await api("GET", "http/cookies?" + q());
    const byDomain = {};
    r.cookies.forEach(c => (byDomain[c.domain] = byDomain[c.domain] || []).push(c));
    const d = openModal("<h3>Cookies</h3><p class=\"faint small\">Cookies servers set are kept and sent back, like a browser (per project). Turn this off in Settings.</p>" +
      (r.cookies.length ? Object.keys(byDomain).sort().map(dom => '<div class="ckdom"><div class="row between"><b>' + esc(dom) + '</b><button class="btn small danger" data-dom="' + esc(dom) + '">Delete all</button></div>' +
        byDomain[dom].map(c => '<div class="ckrow"><code>' + esc(c.name) + "</code><span class=\"mono small\">" + esc(c.value.slice(0, 80)) + '</span><span class="faint tiny">' + esc(c.path) +
          (c.expires ? " · until " + new Date(c.expires * 1000).toLocaleDateString() : " · session") + '</span><button class="icon-btn" data-del="' + esc(dom) + "|" + esc(c.path) + "|" + esc(c.name) + '">' + icon("close") + "</button></div>").join("") + "</div>").join("")
        : '<p class="faint small">No cookies stored.</p>') +
      '<div class="row between"><button class="btn danger" data-a="clear"' + (r.cookies.length ? "" : " disabled") + '>Clear all</button><button class="btn primary" data-a="close">Close</button></div>');
    d.classList.add("wide");
    d.querySelector('[data-a="close"]').onclick = closeModal;
    d.querySelector('[data-a="clear"]').onclick = guard(async () => { await api("DELETE", "http/cookies?" + q()); cookiesDialog(); });
    d.querySelectorAll("[data-dom]").forEach(b => (b.onclick = guard(async () => { await api("DELETE", "http/cookies?" + q() + "&domain=" + encodeURIComponent(b.dataset.dom)); cookiesDialog(); })));
    d.querySelectorAll("[data-del]").forEach(b => (b.onclick = guard(async () => {
      const [dom, path, name] = b.dataset.del.split("|");
      await api("DELETE", "http/cookies?" + q() + "&domain=" + encodeURIComponent(dom) + "&path=" + encodeURIComponent(path) + "&name=" + encodeURIComponent(name));
      cookiesDialog();
    })));
  }
  function settingsDialog() {
    const s = A.scope.settings || {};
    const d = openModal("<h3>API settings</h3>" +
      '<div class="grid2"><label>Request timeout (ms)<input id="stTimeout" inputmode="numeric" value="' + esc(s.timeout_ms || 30000) + '"></label><span></span></div>' +
      '<label class="check"><input type="checkbox" id="stFollow"' + (s.follow !== false ? " checked" : "") + "> Follow redirects</label>" +
      '<label class="check"><input type="checkbox" id="stVerify"' + (s.verify !== false ? " checked" : "") + "> Check TLS certificates (turn off for self-signed local servers)</label>" +
      '<label class="check"><input type="checkbox" id="stCookies"' + (s.cookies !== false ? " checked" : "") + "> Keep cookies and send them back</label>" +
      '<label>Proxy (optional, e.g. http://proxy.company.com:8080)<input id="stProxy" spellcheck="false" value="' + esc(s.proxy || "") + '"></label>' +
      '<p class="faint small">Client certificate for mutual TLS (optional): paths to PEM files on this computer.</p><div class="grid2">' +
      '<label>Certificate file<input id="stCert" spellcheck="false" value="' + esc(s.cert || "") + '" placeholder="C:\\certs\\client.pem"></label>' +
      '<label>Key file<input id="stKey" spellcheck="false" value="' + esc(s.key || "") + '" placeholder="C:\\certs\\client.key"></label>' +
      '<label>Key passphrase<input id="stPass" type="password" value="' + esc(s.passphrase || "") + '"></label></div>' +
      '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="save">Save</button></div>');
    d.classList.add("wide");
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="save"]').onclick = guard(async () => {
      A.scope.settings = (await api("PUT", "http/settings", { timeout_ms: +$("#stTimeout").value || 30000, follow: $("#stFollow").checked, verify: $("#stVerify").checked,
        cookies: $("#stCookies").checked, proxy: $("#stProxy").value, cert: $("#stCert").value, key: $("#stKey").value, passphrase: $("#stPass").value })).settings;
      closeModal();
      toast("Settings saved");
    });
  }
  function schemaDialog(i) {
    const c = tab().req.checks[i];
    const d = openModal('<h3>JSON schema</h3><p class="faint small">The response body must match this schema (types, required fields, enums, lengths, patterns…). OpenAPI imports fill this in for you.</p>' +
      '<textarea id="schemaText" class="code-area" rows="16" spellcheck="false">' + esc(JSON.stringify(c.schema || { type: "object", required: [], properties: {} }, null, 2)) + "</textarea>" +
      '<p class="small" id="schemaMsg"></p><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Done</button></div>');
    d.classList.add("wide");
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="ok"]').onclick = () => {
      try { c.schema = JSON.parse($("#schemaText").value); closeModal(); markDirty(); renderPane(); }
      catch (e) { $("#schemaMsg").textContent = "That isn't valid JSON: " + e.message; $("#schemaMsg").style.color = "var(--red)"; }
    };
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
    await app.newChat();
    if (A.project) await app.patchChat({ project: A.project });
    app.renderChat();
    app.renderRail();
    $("#prompt").value = text;
    app.autosize();
    $("#prompt").focus();
  }
  function pickFile() {
    return new Promise(resolve => {
      const inp = document.createElement("input");
      inp.type = "file";
      inp.onchange = async () => {
        const f = inp.files[0];
        if (!f) return resolve(null);
        if (f.size > 50 * 1024 * 1024) { toast("Files over 50 MB can't be attached.", true); return resolve(null); }
        const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
        resolve((await api("POST", "http/files", { name: f.name, data })).file);
      };
      inp.click();
    });
  }
  function endpointRequest(r) {
    const path = r.path.replace(/\{(\w+)(?::[^}]*)?\}|<(?:\w+:)?(\w+)>/g, (m, a, b) => ":" + (a || b));
    const method = r.method === "ANY" ? "GET" : r.method;
    const req = blank(method + " " + r.path, method, "{{baseUrl}}" + (path.startsWith("/") ? path : "/" + path));
    syncPathVars(req);
    req.path_vars.forEach(p => (p.value = /id$/i.test(p.key) ? "1" : ""));
    if (["POST", "PUT", "PATCH"].includes(method)) req.body = { mode: "json", text: "{\n  \n}", form: [] };
    req.checks = [{ kind: "status", op: "eq", value: "2xx", enabled: true }];
    return req;
  }
  function checksFromResponse(res) {
    const out = [{ kind: "status", op: "eq", value: String(res.status), enabled: true }];
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
  function insertAtCursor(ta, text) {
    ta.focus();
    const before = ta.value.slice(0, ta.selectionStart);
    const pre = before && !before.endsWith("\n") ? "\n" : "";
    document.execCommand("insertText", false, pre + text + "\n");
  }

  // ── scripts from the server, run in this window's sandbox ────────────
  function runScript(ev) {
    let done = false;
    const w = new Worker("/static/pmsandbox.js");
    const finish = result => {
      if (done) return;
      done = true;
      w.terminate();
      api("POST", "http/script", { id: ev.id, result }).catch(() => {});
    };
    const timer = setTimeout(() => finish(Object.assign({}, { scopes: ev.ctx.scopes, request: ev.ctx.request, tests: [], console: [] },
      { error: "The script ran for more than 5 seconds and was stopped." })), 5000);
    w.onmessage = e => { clearTimeout(timer); finish(e.data); };
    w.onerror = e => { clearTimeout(timer); e.preventDefault(); finish({ scopes: ev.ctx.scopes, request: ev.ctx.request, tests: [], console: [], error: e.message || "The script failed." }); };
    w.postMessage(ev.ctx);
  }

  // ── events ───────────────────────────────────────────────────────────
  function onEvent(ev) {
    if (ev.type === "script") return runScript(ev);
    if (ev.type === "api_progress" && A.runs[ev.run]) {
      A.runs[ev.run].push(ev.result);
      const live = $("#runLive");
      if (live) { live.insertAdjacentHTML("beforeend", runRow(ev.result)); live.scrollTop = live.scrollHeight; }
      return;
    }
    if (ev.type === "oauth") {
      toast(ev.ok ? "Signed in: got a new access token." : "Sign-in failed: " + ev.error, !ev.ok);
      return;
    }
    if (ev.type === "rt" || ev.type === "rt_closed") {
      const t = A.tabs.find(x => x.rt && x.rt.sid === ev.id);
      if (!t) {
        (A.rtEarly[ev.id] = A.rtEarly[ev.id] || []).push(ev);
        setTimeout(() => delete A.rtEarly[ev.id], 10000);
        return;
      }
      if (ev.type === "rt_closed") { t.rt.sid = ""; t.rt.log.push({ dir: "sys", data: "Disconnected", ts: Date.now() / 1000 }); if (tab() === t) renderEditor(); return; }
      t.rt.log.push(ev);
      if (t.rt.log.length > 2000) t.rt.log.splice(0, 500);
      if (tab() === t) {
        const l = $("#rtLog");
        if (l) { const stick = l.scrollHeight - l.scrollTop - l.clientHeight < 40; if (t.rt.log.length === 1) l.innerHTML = ""; l.insertAdjacentHTML("beforeend", rtLine(ev)); if (stick) l.scrollTop = l.scrollHeight; }
      }
      return;
    }
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
    $("#apiProject").onchange = guard(async () => {
      if (A.tabs.some(t => t.dirty && t.saved) && !confirm("Discard unsaved changes in open tabs?")) { $("#apiProject").value = A.project; return; }
      A.project = $("#apiProject").value;
      store("va-api-project", A.project);
      A.routes = null;
      A.tabs = [];
      newTab();
      await load();
      renderAll();
    });
    $("#apiEnv").onchange = guard(async () => { await api("POST", "http/active_env", { project: A.project, env: $("#apiEnv").value }); A.scope.active_env = $("#apiEnv").value; });
    $("#apiEnvEdit").onclick = () => envDialog();
    $("#apiImport").onclick = importDialog;
    $("#apiCookies").onclick = guard(cookiesDialog);
    $("#apiSettings").onclick = settingsDialog;
    $("#apiNew").onclick = () => {
      const d = openModal('<h3>New</h3><div class="menu-list"><button data-n="req">' + icon("plus") + "HTTP request</button><button data-n=\"ws\">" + icon("chevron-up") + "WebSocket</button>" +
        '<button data-n="sse">' + icon("chevron-down") + "Server-Sent Events</button><button data-n=\"col\">" + icon("folder-plus") + "Collection</button><button data-n=\"env\">" + icon("code") + "Environment</button></div>");
      d.querySelectorAll("[data-n]").forEach(b => (b.onclick = guard(async () => {
        closeModal();
        const k = b.dataset.n;
        if (k === "req") { newTab(); renderAll(); $("#reqUrl").focus(); }
        else if (k === "ws" || k === "sse") { const t = newTab(blank(k === "ws" ? "WebSocket" : "Event stream", "GET", k === "ws" ? "ws://localhost:3000/" : "{{baseUrl}}/events")); t.req.kind = k; t.req.checks = []; t.reqTab = "headers"; renderAll(); }
        else if (k === "col") {
          const name = prompt("Name for the collection:", "Users API");
          if (!name) return;
          const c = (await api("POST", "http/collections", { project: A.project, name })).collection;
          A.scope.collections.push(c);
          A.open.add(c.id);
          renderSide();
        } else envDialog();
      })));
    };
    $("#apiFilter").oninput = () => { A.filter = $("#apiFilter").value.trim(); renderSide(); };
    $$(".api-side .tab").forEach(t => (t.onclick = guard(async () => {
      A.sideTab = t.dataset.aside;
      if (A.sideTab === "endpoints" && !A.routes && A.project) { renderSide(); A.routes = (await api("GET", "http/endpoints?" + q())).routes; }
      renderSide();
    })));
    // tabs
    $("#apiTabs").addEventListener("click", e => {
      const c = e.target.closest("[data-close]");
      if (c) { e.stopPropagation(); closeTab(+c.dataset.close); return; }
      if (e.target.closest('[data-a="newtab"]')) { newTab(); renderAll(); $("#reqUrl").focus(); return; }
      const t = e.target.closest("[data-tab]");
      if (t) { A.active = +t.dataset.tab; renderTabs(); renderEditor(); renderSide(); }
    });
    $("#apiTabs").addEventListener("auxclick", e => { const t = e.target.closest("[data-tab]"); if (t && e.button === 1) closeTab(+t.dataset.tab); });
    // side panel
    const side = $("#apiSide");
    side.addEventListener("click", guard(async e => {
      const b = e.target.closest("[data-a]");
      const row = e.target.closest("[data-node]");
      const col = row && colById(row.dataset.col);
      if (b) {
        const a = b.dataset.a;
        if (a === "newcol") { $("#apiNew").click(); return; }
        if (a === "import") return importDialog();
        if (a === "clearhist") { if (confirm("Clear the request history?")) { await api("DELETE", "http/history?" + q()); await load(); renderSide(); } return; }
        if (a === "scan") { A.routes = null; renderSide(); A.routes = (await api("GET", "http/endpoints?" + q())).routes; renderSide(); toast("Found " + A.routes.length + " endpoints"); return; }
        if (a === "addall") {
          const reqs = A.routes.map(endpointRequest);
          const groups = {};
          reqs.forEach(r => { const seg = (r.url.replace("{{baseUrl}}", "").split("/").filter(Boolean).find(s => !/^(api|v\d+)$/.test(s) && !s.startsWith(":")) || "root"); (groups[seg] = groups[seg] || []).push(r); });
          const items = Object.keys(groups).length > 1 ? Object.keys(groups).map(g => ({ id: uid(), name: g, folder: true, items: groups[g], auth: { type: "inherit" }, pre_script: "", test_script: "" })) : reqs;
          const c = (await api("POST", "http/collections", { project: A.project, name: "Endpoints", items })).collection;
          A.scope.collections.push(c);
          A.open.add(c.id);
          A.sideTab = "collections";
          renderAll();
          toast("Added " + reqs.length + " requests, in folders by resource. Fill in path values and bodies, then run it.");
          return;
        }
        if (a === "compare") return compareDialog();
        if (!col) return;
        e.stopPropagation();
        const nodeId = row.dataset.node;
        if (a === "run") return runDialog(col, nodeId === col.id ? null : find(col.items, nodeId).node);
        if (a === "add") { A.open.add(nodeId); A.open.add(col.id); newTab(null, col.id, nodeId === col.id ? "" : nodeId); renderAll(); $("#reqUrl").focus(); return; }
        if (a === "more") return nodeMenu(col, nodeId);
        return;
      }
      if (row && col) {
        const nodeId = row.dataset.node;
        if (nodeId === col.id || row.classList.contains("fold")) {
          if (e.target.closest(".tw") || e.detail === 1) { A.open.has(nodeId) ? A.open.delete(nodeId) : A.open.add(nodeId); renderSide(); }
          return;
        }
        openNode(col.id, nodeId);
        return;
      }
      const hi = e.target.closest("[data-hist]");
      if (hi) {
        const x = A.scope.history.find(y => y.id === hi.dataset.hist);
        if (x) { newTab(Object.assign(blank(), clone(x.request), { id: uid(), name: x.request.name || x.request.url, checks: [] })); renderAll(); }
        return;
      }
      const ep = e.target.closest("[data-ep]");
      if (ep) { newTab(endpointRequest(A.routes[+ep.dataset.ep])); renderAll(); }
    }));
    side.addEventListener("dblclick", e => {
      const row = e.target.closest(".tn.coll, .tn.fold");
      if (row) openNode(row.dataset.col, row.dataset.node);
    });
    // drag and drop in the tree
    side.addEventListener("dragstart", e => {
      const row = e.target.closest("[data-node]");
      if (!row || row.classList.contains("coll")) return;
      e.dataTransfer.setData("text/x-api-node", row.dataset.col + "|" + row.dataset.node);
      e.dataTransfer.effectAllowed = "move";
    });
    side.addEventListener("dragover", e => {
      const row = e.target.closest("[data-node]");
      if (!row || !e.dataTransfer.types.includes("text/x-api-node")) return;
      e.preventDefault();
      $$(".tn.drop-into, .tn.drop-before").forEach(x => x.classList.remove("drop-into", "drop-before"));
      row.classList.add(row.classList.contains("rq") ? "drop-before" : "drop-into");
    });
    side.addEventListener("dragleave", e => { const row = e.target.closest("[data-node]"); if (row) row.classList.remove("drop-into", "drop-before"); });
    side.addEventListener("drop", guard(async e => {
      const row = e.target.closest("[data-node]");
      const data = e.dataTransfer.getData("text/x-api-node");
      $$(".tn.drop-into, .tn.drop-before").forEach(x => x.classList.remove("drop-into", "drop-before"));
      if (!row || !data) return;
      e.preventDefault();
      const [srcColId, nodeId] = data.split("|");
      const dst = colById(row.dataset.col);
      await moveNode(colById(srcColId), nodeId, dst, row.dataset.node, row.classList.contains("rq") ? "before" : "into");
    }));
    // editor
    const ed = $("#apiEditor");
    ed.addEventListener("input", e => {
      const t = tab();
      if (!t) return;
      const el = e.target;
      if (t.type !== "request") {
        if (el.id === "nodeName") t.node.name = el.value;
        else if (el.id === "nodeDesc") t.node.description = el.value;
        else if (el.dataset.script) t.node[el.dataset.script] = el.value;
        else if (el.closest(".auth-form") && el.tagName === "INPUT" && el.type !== "checkbox") t.node.auth = readAuth(el.closest(".auth-form"), t.node.auth);
        else if (el.closest('.kv[data-kv="variables"]')) { t.node.variables = readKV(el.closest(".kv")); if (el.closest(".kvr.new")) { renderNodeEditor(t); refocusLast("variables", el.dataset.f); } }
        markDirty();
        return;
      }
      const r = t.req;
      if (el.id === "reqUrl") {
        let v = el.value;
        const i = v.indexOf("?");
        if (i >= 0 && !v.slice(i).includes("{{") && !r.kind) {
          new URLSearchParams(v.slice(i + 1)).forEach((value, key) => r.params.push({ key, value, enabled: true }));
          v = v.slice(0, i);
          el.value = v;
          t.reqTab = "params";
        }
        r.url = v;
        const before = (r.path_vars || []).map(p => p.key).join();
        syncPathVars(r);
        if (before !== r.path_vars.map(p => p.key).join() && t.reqTab === "params") renderPane();
      } else if (el.id === "reqName") r.name = el.value;
      else if (el.id === "bodyText") r.body.text = el.value;
      else if (el.id === "gqlQuery") r.body.query = el.value;
      else if (el.id === "gqlVars") r.body.variables = el.value;
      else if (el.id === "rtProto") r.protocols = el.value;
      else if (el.id === "rtMsg") { t.rt.draft = el.value; return; }
      else if (el.id === "resFind") { t.find = el.value; const pos = el.selectionStart; renderRes(); const f = $("#resFind"); f.focus(); f.setSelectionRange(pos, pos); return; }
      else if (el.dataset.script) r[el.dataset.script] = el.value;
      else if (el.dataset.set === "timeout_ms") { r.settings = r.settings || {}; r.settings.timeout_ms = parseInt(el.value, 10) || ""; }
      else if (el.closest(".auth-form") && el.tagName === "INPUT" && el.type !== "checkbox") r.auth = readAuth(el.closest(".auth-form"), r.auth);
      else if (el.closest(".kv")) {
        const kv = el.closest(".kv"), kind = kv.dataset.kv;
        const rows = readKV(kv);
        if (kind === "form") rows.forEach((x, i) => { const old = (r.body.form || [])[i]; if (old && old.file) x.file = old.file; });
        if (kind === "form") r.body.form = rows; else r[kind] = rows;
        if (el.closest(".kvr.new")) { renderPane(); refocusLast(kind, el.dataset.f); }
      } else if (el.closest(".ck") && el.dataset.k && el.tagName === "INPUT" && el.type !== "checkbox") r.checks[+el.closest(".ck").dataset.ck][el.dataset.k] = el.value;
      else return;
      markDirty();
    });
    // only the auth type and grant change which fields show; re-drawing on other changes would steal focus
    const relayout = el => el.dataset.auth === "type" || el.dataset.auth === "grant";
    ed.addEventListener("change", e => {
      const t = tab();
      if (!t) return;
      const el = e.target;
      if (t.type !== "request") {
        if (el.closest(".auth-form")) { t.node.auth = readAuth(el.closest(".auth-form"), t.node.auth); if (relayout(el)) renderNodeEditor(t); }
        else if (el.closest('.kv[data-kv="variables"]')) t.node.variables = readKV(el.closest(".kv"));
        markDirty();
        return;
      }
      const r = t.req;
      if (el.id === "reqMethod") {
        const v = el.value;
        if (v === "WS" || v === "SSE") { r.kind = v.toLowerCase(); if (r.kind === "ws" && /^https?:/.test(r.url)) r.url = r.url.replace(/^http/, "ws"); t.reqTab = "headers"; }
        else { delete r.kind; r.method = v; if (/^wss?:/.test(r.url)) r.url = r.url.replace(/^ws/, "http"); if (t.reqTab === "proto") t.reqTab = "params"; }
        markDirty(); renderTabs(); renderEditor(); return;
      }
      if (el.name === "bmode") {
        r.body.mode = el.value;
        if (el.value === "json" && !r.body.text) r.body.text = "{\n  \n}";
        if ((el.value === "form" || el.value === "multipart") && !r.body.form) r.body.form = [];
        markDirty(); renderPane(); return;
      }
      if (el.closest(".auth-form")) { r.auth = readAuth(el.closest(".auth-form"), r.auth); markDirty(); if (relayout(el)) renderPane(); return; }
      if (el.dataset.set && el.dataset.set !== "timeout_ms") { r.settings = r.settings || {}; r.settings[el.dataset.set] = el.value === "" ? undefined : el.value === "on"; markDirty(); return; }
      if (el.id === "resRaw") { t.raw = el.checked; renderRes(); return; }
      const kv = el.closest(".kv");
      if (kv) {
        const rows = readKV(kv);
        if (kv.dataset.kv === "form") { rows.forEach((x, i) => { const old = (r.body.form || [])[i]; if (old && old.file) x.file = old.file; }); r.body.form = rows; if (el.dataset.f === "type") renderPane(); }
        else r[kv.dataset.kv] = rows;
        markDirty(); return;
      }
      const ck = el.closest(".ck");
      if (ck) {
        const c = r.checks[+ck.dataset.ck], k = el.dataset.k;
        c[k] = el.type === "checkbox" ? el.checked : el.value;
        if (k === "kind") { c.op = CHECK_OPS[c.kind][0][0]; c.value = c.kind === "status" ? "200" : c.kind === "time" ? "500" : ""; if (c.kind === "schema" && !c.schema) c.schema = { type: "object" }; }
        markDirty();
        if (k === "kind" || k === "op") renderPane();
      }
    });
    ed.addEventListener("click", guard(async e => {
      const t = tab();
      if (!t) return;
      const el = e.target;
      const id = (el.closest("button") || {}).id;
      if (id === "reqSend") return send();
      if (id === "rtConnect") return toggleRT();
      if (id === "reqSave") return save(false);
      if (id === "reqSaveAs") return save(true);
      if (id === "reqCode") return codeDialog();
      if (id === "nodeSave") return saveNode(t);
      if (id === "rtSend") {
        const text = $("#rtMsg").value;
        if (!text) return;
        await api("POST", "http/rt", { action: "send", id: t.rt.sid, text });
        t.rt.draft = "";
        $("#rtMsg").value = "";
        return;
      }
      const rtab = el.closest("[data-rtab]");
      if (rtab) { t.reqTab = rtab.dataset.rtab; $$("#apiEditor .req-tabs .tab").forEach(b => b.classList.toggle("active", b === rtab)); renderPane(); return; }
      const ns = el.closest("[data-nsub]");
      if (ns) { t.sub = ns.dataset.nsub; renderNodeEditor(t); return; }
      const st = el.closest("[data-stab]");
      if (st) { t.scriptTab = st.dataset.stab; if (t.type === "request") renderPane(); else renderNodeEditor(t); return; }
      const sn = el.closest("[data-snip]");
      if (sn) { const [w, i] = sn.dataset.snip.split(":"); insertAtCursor($("#apiEditor textarea[data-script]"), SNIPPETS[w][+i][1]); return; }
      const rt = el.closest("[data-rt]");
      if (rt) { t.resTab = rt.dataset.rt; renderRes(); return; }
      if (el.closest("[data-rtclear]")) { t.rt.log = []; renderRT(); return; }
      const kvdel = el.closest("[data-kvdel]");
      if (kvdel) {
        const kind = kvdel.closest(".kv").dataset.kv;
        const list = t.type !== "request" ? t.node.variables : kind === "form" ? t.req.body.form : t.req[kind];
        list.splice(+kvdel.dataset.kvdel, 1);
        markDirty();
        if (t.type === "request") renderPane(); else renderNodeEditor(t);
        return;
      }
      const ckdel = el.closest("[data-ckdel]");
      if (ckdel) { t.req.checks.splice(+ckdel.dataset.ckdel, 1); markDirty(); renderPane(); return; }
      const exdel = el.closest("[data-exdel]");
      if (exdel) {
        e.preventDefault();
        t.req.examples.splice(+exdel.dataset.exdel, 1);
        if (!t.req.examples.length) t.reqTab = "params";
        markDirty(); renderEditor(); return;
      }
      const es = el.closest("[data-editschema]");
      if (es) return schemaDialog(+es.dataset.editschema);
      const pf = el.closest("[data-pickfile]");
      if (pf) {
        const file = await pickFile();
        if (!file) return;
        if (pf.dataset.pickfile === "binary") t.req.body.file = file;
        else { const row = t.req.body.form[+pf.dataset.pickfile]; row.file = file; row.type = "file"; }
        markDirty(); renderPane(); return;
      }
      const oa = el.closest("[data-oauth]");
      if (oa) {
        const body = { project: A.project, collection: t.colId, parent: t.parent, request: t.type === "request" ? t.req : Object.assign(blank(), { auth: t.node.auth }), action: oa.dataset.oauth };
        if (t.type !== "request") { body.collection = ""; body.request.auth = t.node.auth; }
        const r = await api("POST", "http/oauth", body);
        if (oa.dataset.oauth === "clear") toast("Token cleared");
        else if (r.done) toast("Got a new access token (" + r.token + ")");
        else if (r.url) { window.open(r.url, "va-oauth", "width=520,height=720"); toast("Sign in in the window that opened. The token is saved when you're done."); }
        return;
      }
      const nb = el.closest("[data-n]");
      if (nb) {
        const col = colById(t.colId);
        const n = nb.dataset.n;
        if (n === "run") return runDialog(col, t.type === "folder" ? find(col.items, t.nodeId).node : null);
        if (n === "export") return app.download("http/collections/" + col.id + "/export");
        if (n === "share" || n === "unshare") {
          const r = await api("POST", "http/collections/" + col.id + "/move", { project: A.project, to: n === "share" ? "project" : "app" });
          Object.assign(col, r.collection);
          toast(n === "share" ? "Stored in " + col.file + ". Commit it to share it with your team." : "Moved back into the app.");
          await load(); renderAll();
        }
        return;
      }
      const b = el.closest("[data-b]");
      if (b) {
        const r = t.req;
        if (b.dataset.b === "format") {
          const key = r.body.mode === "graphql" ? "variables" : "text";
          try { r.body[key] = JSON.stringify(JSON.parse(r.body[key] || "{}"), null, 2); renderPane(); } catch (err) { toast("That isn't valid JSON: " + err.message, true); }
        } else if (b.dataset.b === "addcheck") { r.checks.push({ kind: "json", op: "exists", path: "", value: "", enabled: true }); markDirty(); renderPane(); }
        else if (b.dataset.b === "suggestchecks") { r.checks = checksFromResponse(t.res); markDirty(); renderPane(); toast("Added checks that match this response. Adjust them as you like."); }
        return;
      }
      const ra = el.closest("[data-r]");
      if (ra) {
        const res = t.res;
        if (ra.dataset.r === "example") {
          const name = prompt("Name for this example:", res.status + " " + (res.reason || ""));
          if (!name) return;
          t.req.examples = (t.req.examples || []).concat([{ name, status: res.status, reason: res.reason, headers: res.headers,
            body: res.json != null ? JSON.stringify(res.json, null, 2) : res.body || "" }]);
          markDirty();
          if (t.saved) await save(false);
          else { renderEditor(); toast("Example added. Save the request to keep it."); }
        } else if (ra.dataset.r === "file") {
          const blob = res.body_b64 ? new Blob([Uint8Array.from(atob(res.body_b64), c => c.charCodeAt(0))], { type: res.content_type || "application/octet-stream" })
            : new Blob([res.json != null ? JSON.stringify(res.json, null, 2) : res.body || ""], { type: res.content_type || "text/plain" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = (t.req.name || "response").replace(/[^\w.-]+/g, "_") + (res.json != null ? ".json" : /html/.test(res.content_type || "") ? ".html" : "");
          a.click();
          setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        } else if (ra.dataset.r === "ai") {
          const body = res.json != null ? JSON.stringify(res.json, null, 2) : (res.body || "");
          const failed = (res.checks || []).filter(c => !c.pass).map(c => c.label + " (got " + c.actual + ")").concat((res.tests || []).filter(x => !x.passed).map(x => x.name + ": " + x.error));
          await askAI("I sent " + t.req.method + " " + (res.url || t.req.url) + " and got " + (res.error ? "an error: " + res.error : res.status + " " + (res.reason || "") + " in " + res.ms + " ms") +
            ".\n" + (failed.length ? "Failing: " + failed.join("; ") + "\n" : "") + ((res.script_errors || []).length ? "Script errors: " + res.script_errors.join("; ") + "\n" : "") +
            (body ? "Response body:\n```\n" + body.slice(0, 2500) + "\n```\n" : "") + "\nExplain what this means" + (A.project ? " and, if something is wrong, find the cause in the code and fix it" : "") + ".");
        }
      }
    }));
    ed.addEventListener("keydown", e => {
      if (e.target.classList.contains("code-area") && e.key === "Tab" && !e.ctrlKey) {
        e.preventDefault();
        document.execCommand("insertText", false, "    ");
      }
      if (e.target.id === "rtMsg" && (e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); e.stopPropagation(); $("#rtSend").click(); }
    });
    // splitter between the request and the response
    ed.addEventListener("pointerdown", e => {
      if (e.target.id !== "apiSplit") return;
      const pane = $("#reqPane");
      const startY = e.clientY, startH = pane.getBoundingClientRect().height;
      const move = ev => { const h = Math.max(80, Math.min(window.innerHeight * 0.75, startH + ev.clientY - startY)); pane.style.maxHeight = pane.style.height = h + "px"; A.paneH = h; };
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    $("#view-api").addEventListener("keydown", e => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "Enter") { e.preventDefault(); guard(send)(); }
      else if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); guard(() => save(false))(); }
      else if (mod && e.key.toLowerCase() === "t" && !e.shiftKey) { e.preventDefault(); newTab(); renderAll(); $("#reqUrl").focus(); }
      else if (mod && e.key.toLowerCase() === "w") { e.preventDefault(); closeTab(A.active); }
    });
    document.addEventListener("paste", e => {
      if (e.target.id !== "reqUrl") return;
      const text = (e.clipboardData || window.clipboardData).getData("text");
      if (!/^\s*curl\s/.test(text)) return;
      e.preventDefault();
      guard(async () => {
        const r = await api("POST", "http/import", { project: A.project, text, collection: tab().colId || "" });
        await load();
        A.open.add(r.collection.id);
        newTab(r.request, r.collection.id, "");
        tab().dirty = false;
        tab().saved = true;
        renderAll();
        toast("Imported the curl command");
      })();
    });
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
  function refocusLast(kind, field) {
    const rows = $$('#apiEditor .kv[data-kv="' + kind + '"] .kvr:not(.new) [data-f="' + (field || "key") + '"]');
    const el = rows[rows.length - 1];
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  }

  wire();
  return { show, onEvent, leaving: () => !A.tabs.some(t => t.dirty && t.saved) || confirm("Discard unsaved changes in the API tab?") };
};
