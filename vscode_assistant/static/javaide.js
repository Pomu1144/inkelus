// Code intelligence in the Projects editor, through language servers on this computer: Java with the Eclipse
// JDT language server (javals.py), TypeScript/JavaScript/React with typescript-language-server plus ESLint
// (tsls.py). Errors as you type, completion, hover, parameter hints, go to definition (into libraries too),
// find usages, rename, quick fixes, organize imports, formatting (Prettier when the project uses it), the
// outline, a Problems list, Run/Debug links on tests, and the Java debugger. Keys follow NetBeans as well as
// VS Code. app.js calls VAJava(app, Projects) once.
window.VAJava = function (app, Projects) {
  "use strict";
  const { api, toast, guard, openModal, closeModal, $, $$ } = app;
  const esc = v => app.esc(v == null ? "" : String(v));
  const P = Projects.state;
  const J = {
    tools: null, info: {},            // Java: project id -> {state, message, root_uri, counts}
    ts: { tools: null, info: {}, eslint: {}, uses: {}, progress: "" },   // TypeScript and ESLint, the same per project
    diags: {},                        // project id -> lang -> {uri: [lsp diagnostic]}
    versions: {},                     // lsp uri -> version sent
    opened: new Set(),                // "lang uri" the servers have open
    timers: {}, isJava: {}, isTs: {}, progress: "", usages: null, outline: null, virtual: {},
  };
  const Ed = () => Projects.editor;
  const M = () => window.monaco;
  const pid = () => P.cur;

  // ── languages ────────────────────────────────────────────────────────
  const isJavaPath = p => /\.java$/i.test(p || "");
  const isTsPath = p => /\.(tsx?|mts|cts|jsx?|mjs|cjs)$/i.test(p || "");
  const langOf = f => (!f ? null : f.jdt ? "java" : isJavaPath(f.path) ? "java" : isTsPath(f.path) ? "ts" : null);
  const LABEL = { java: "Java", ts: "TypeScript", eslint: "ESLint" };
  const infoOf = (id, lang) => (lang === "ts" ? J.ts.info : lang === "eslint" ? J.ts.eslint : J.info)[id || pid()];
  const running = (id, lang) => ["ready", "importing"].includes((infoOf(id, lang || "java") || {}).state);
  // the servers a file talks to: TypeScript files also go to ESLint when the project uses it
  const serversFor = f => { const l = langOf(f); return l === "ts" ? ["ts", "eslint"] : l ? [l] : []; };

  // ── paths and URIs ───────────────────────────────────────────────────
  // the same escaping as Python's quote(path, safe="/:") on the server
  const quote = s => encodeURIComponent(s).replace(/[!'()*]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase()).replace(/%2F/g, "/").replace(/%3A/g, ":");
  const rootUri = (id) => ((J.info[id || pid()] || J.ts.info[id || pid()]) || {}).root_uri || "";
  const lspUri = (id, path) => rootUri(id) + "/" + path.split("/").map(quote).join("/");
  function relPath(id, uri) {
    const root = rootUri(id);
    if (!root || !uri.startsWith(root + "/")) return null;
    return uri.slice(root.length + 1).split("/").map(decodeURIComponent).join("/");
  }
  const fileByKey = key => P.open.find(f => Projects.keyOf(f) === key);
  function lspDocOf(model) {
    const key = Ed().keyOfUri(model.uri);
    if (!key) return null;
    const f = fileByKey(key);
    const lang = langOf(f);
    if (!lang) return null;
    if (f.jdt) return { project: f.project, uri: f.jdt, file: f, lang };
    return { project: f.project, uri: lspUri(f.project, f.path), file: f, lang };
  }
  const toRange = r => new (M().Range)(r.start.line + 1, r.start.character + 1, r.end.line + 1, r.end.character + 1);
  const toPos = p => ({ line: p.lineNumber - 1, character: p.column - 1 });

  async function call(method, params, id, timeout, lang) {
    return (await api("POST", "java", { action: "call", lang: lang || "java", project: id || pid(), method, params, timeout })).result;
  }
  const post = (lang, body) => api("POST", "java", Object.assign({ lang }, body));

  // ── status and setup ─────────────────────────────────────────────────
  async function refreshStatus(start) {
    const id = pid();
    if (!id) return;
    const r = await api("GET", "java?project=" + encodeURIComponent(id));
    if (!J.reset) J.reset = new Set();
    const t = r.ts || {};
    for (const [lang, info] of [["java", r.server], ["ts", t.server], ["eslint", t.eslint]]) {
      if (info && ["ready", "importing"].includes(info.state) && !J.reset.has(lang + id)) {
        J.reset.add(lang + id);   // the server may still hold copies of files from before this page loaded
        await post(lang, { action: "sync", project: id, op: "reset", uri: "" });
      }
    }
    J.tools = r.tools;
    J.isJava[id] = r.is_java;
    J.info[id] = r.server;
    J.ts.tools = t.tools;
    J.isTs[id] = !!t.is_ts;
    J.ts.info[id] = t.server;
    J.ts.eslint[id] = t.eslint;
    J.ts.uses[id] = !!t.uses_eslint;
    J.diags[id] = { java: {}, ts: {}, eslint: {} };
    [["java", r.server], ["ts", t.server], ["eslint", t.eslint]].forEach(([lang, info]) =>
      ((info || {}).diagnostics || []).forEach(d => (J.diags[id][lang][d.uri] = d.items)));
    if (start && r.is_java && r.tools.installed && r.server.state === "stopped") {
      J.info[id] = (await api("POST", "java", { action: "start", project: id })).server;
    }
    if (start && t.is_ts && t.tools.installed && t.server.state === "stopped") {
      J.ts.info[id] = (await post("ts", { action: "start", project: id })).server;
    }
    renderBar();
    renderProblems();
    allMarkers();
  }
  function renderBar() {
    const id = pid(), bar = $("#javaBar");
    const isJava = !!J.isJava[id], isTs = !!J.isTs[id];
    bar.hidden = !isJava && !isTs;
    $("#outlineTab").hidden = !isJava && !isTs;
    $$('#panelTabs [data-panel="problems"], #panelTabs [data-panel="usages"]').forEach(t => (t.hidden = !isJava && !isTs));
    if (bar.hidden) return;
    const c = counts(id);
    const probs = (c.error ? '<button class="linkish jerr" data-j="problems">' + c.error + " error" + (c.error === 1 ? "" : "s") + "</button>" : "") +
      (c.warning ? '<button class="linkish jwarn" data-j="problems">' + c.warning + " warning" + (c.warning === 1 ? "" : "s") + "</button>" : "");
    const seg = (lang, t, s, progress, intro, setupLabel, size) => {
      let h = '<span class="jlogo">' + icon("code") + LABEL[lang] + "</span>";
      if (!t.installed) {
        h += t.busy ? '<span class="spinner"></span><span>' + esc(progress || "Installing…") + "</span>"
          : t.problem ? '<span class="jerr">' + esc(t.problem) + "</span>"
            : "<span>" + intro + '</span><button class="btn small primary" data-j="' + lang + '-setup">' + setupLabel + '</button><span class="faint tiny">' + size + "</span>";
        if (t.error) h += '<span class="jerr">' + esc(t.error) + "</span>";
      } else if (s.state === "ready") {
        h += '<span class="jok">' + esc(s.message && s.message !== "Ready" ? s.message : "Ready") + "</span>";
      } else if (s.state === "starting" || s.state === "importing") {
        h += '<span class="spinner"></span><span class="faint">' + esc(progress || s.message || "Starting…") + "</span>";
      } else if (s.state === "error") {
        h += '<span class="jerr">' + esc(s.message) + '</span><button class="btn small" data-j="' + lang + '-restart">Restart</button>';
      } else {
        h += '<span class="faint">Not running</span><button class="btn small" data-j="' + lang + '-start">Start</button>';
      }
      if (t.installed && s.state === "ready") h += '<button class="icon-btn" data-j="' + lang + '-restart" title="Restart the ' + LABEL[lang] + ' language server">' + icon("refresh") + "</button>";
      return '<span class="jseg">' + h + "</span>";
    };
    let h = "";
    if (isJava) h += seg("java", J.tools || {}, J.info[id] || {}, J.progress, "Errors as you type, completion, go to definition, find usages, rename, quick fixes and a debugger.",
      "Set up Java support", "One-time download, about 50 MB" + ((J.tools || {}).java ? " · uses Java " + esc(J.tools.java_version) : ""));
    if (isTs) {
      h += seg("ts", J.ts.tools || {}, J.ts.info[id] || {}, J.ts.progress, "Errors as you type, completion, go to definition, rename, auto-imports, ESLint and Prettier.",
        "Set up TypeScript support", "One-time npm install, about 30 MB" + ((J.ts.tools || {}).node ? " · uses Node " + esc(J.ts.tools.node_version) : ""));
      const e = J.ts.eslint[id] || {};
      if (J.ts.uses[id] && (J.ts.tools || {}).installed) h += '<span class="faint tiny">ESLint ' + (e.state === "ready" ? "on" : e.state === "error" ? '<span class="jerr">' + esc(e.message) + "</span>" : esc(e.state || "off")) + "</span>";
    }
    const anyReady = (isJava && running(id, "java")) || (isTs && running(id, "ts"));
    if (anyReady) h += probs || '<span class="faint">No problems</span>';
    if (anyReady) h += '<span class="jkeys faint tiny">Alt+Enter fix · Ctrl+Space complete · F12/Ctrl+B definition · Alt+F7 usages · Ctrl+R rename · Ctrl+O go to type</span>';
    bar.innerHTML = h;
  }

  // ── keeping the servers' copies of open files in step ───────────────
  function syncOpen(f) {
    if (!f || f.jdt || f.text == null) return;
    serversFor(f).forEach(lang => {
      if (!running(f.project, lang)) return;
      const uri = lspUri(f.project, f.path), k = lang + " " + uri;
      if (J.opened.has(k)) return;
      J.opened.add(k);
      J.versions[uri] = J.versions[uri] || 1;
      post(lang, { action: "sync", project: f.project, op: "open", uri, text: Ed().value(Projects.keyOf(f)) ?? f.text, version: J.versions[uri] }).catch(() => J.opened.delete(k));
    });
  }
  function sendChange(f, now) {
    const uri = lspUri(f.project, f.path);
    J.versions[uri] = (J.versions[uri] || 1) + 1;
    const text = Ed().value(Projects.keyOf(f));
    return Promise.all(serversFor(f).filter(l => running(f.project, l) && J.opened.has(l + " " + uri))
      .map(lang => post(lang, { action: "sync", project: f.project, op: "change", uri, text, version: J.versions[uri] }).catch(() => {})));
  }
  function syncChange(key) {
    const f = fileByKey(key);
    if (!f || f.jdt || !langOf(f)) return;
    const uri = lspUri(f.project, f.path);
    if (serversFor(f).some(l => running(f.project, l) && !J.opened.has(l + " " + uri))) syncOpen(f);
    clearTimeout(J.timers[uri]);
    J.timers[uri] = setTimeout(() => sendChange(f), 200);
  }
  async function flush(f) {          // send pending typing before asking the server something
    const uri = lspUri(f.project, f.path);
    if (J.timers[uri]) { clearTimeout(J.timers[uri]); J.timers[uri] = null; await sendChange(f); }
  }
  function syncAllOpen() { P.open.filter(f => f.project === pid()).forEach(syncOpen); }

  // ── diagnostics: red underlines and the Problems list ───────────────
  const SEV = d => [null, "error", "warning", "info", "hint"][d.severity || 1];
  const diagsOf = (id, uri, lang) => {
    const all = J.diags[id] || {};
    return lang ? (all[lang] || {})[uri] || [] : ["java", "ts", "eslint"].flatMap(l => (all[l] || {})[uri] || []);
  };
  function markersFor(id, path, model) {
    const items = diagsOf(id, lspUri(id, path));
    const S = M().MarkerSeverity;
    M().editor.setModelMarkers(model, "lsp", items.map(d => Object.assign({
      severity: { error: S.Error, warning: S.Warning, info: S.Info, hint: S.Hint }[SEV(d)], message: d.message,
      source: d.source || (isJavaPath(path) ? "Java" : "TypeScript"), code: d.code != null ? String(d.code) : undefined,
      tags: (d.tags || []).map(t => (t === 1 ? M().MarkerTag.Unnecessary : M().MarkerTag.Deprecated)),
    }, { startLineNumber: d.range.start.line + 1, startColumn: d.range.start.character + 1, endLineNumber: d.range.end.line + 1, endColumn: d.range.end.character + 1 })));
  }
  function allMarkers() {
    if (!M()) return;
    P.open.forEach(f => { if (!f.jdt && langOf(f)) { const m = Ed().model(Projects.keyOf(f)); if (m) markersFor(f.project, f.path, m); } });
  }
  function counts(id) {
    let error = 0, warning = 0;
    Object.values(J.diags[id] || {}).forEach(byUri => Object.entries(byUri).forEach(([uri, items]) => {
      if (!relPath(id, uri)) return;
      items.forEach(d => { const s = SEV(d); if (s === "error") error++; else if (s === "warning") warning++; });
    }));
    return { error, warning };
  }
  function renderProblems() {
    const id = pid(), all = J.diags[id] || {};
    const rows = [];
    const uris = [...new Set(Object.values(all).flatMap(o => Object.keys(o)))].sort();
    uris.forEach(uri => {
      const path = relPath(id, uri);
      if (!path) return;
      diagsOf(id, uri).slice().sort((a, b) => (a.severity || 1) - (b.severity || 1) || a.range.start.line - b.range.start.line).forEach(d => {
        const sev = SEV(d);
        if (sev !== "error" && sev !== "warning") return;
        rows.push('<div class="prow ' + sev + '" data-path="' + esc(path) + '" data-line="' + (d.range.start.line + 1) + '" data-col="' + (d.range.start.character + 1) + '">' +
          icon(sev === "error" ? "cross" : "dot") + '<span class="pmsg">' + esc(d.message) + (d.source === "eslint" && d.code ? ' <span class="faint tiny">' + esc(d.code) + "</span>" : "") +
          '</span><span class="faint mono tiny">' + esc(path.split("/").pop()) + ":" + (d.range.start.line + 1) + "</span></div>");
      });
    });
    const c = counts(id);
    $("#probCount").textContent = c.error + c.warning ? String(c.error + c.warning) : "";
    const any = running(id, "java") || running(id, "ts");
    $("#problemsPane").innerHTML = rows.length ? rows.join("") : '<p class="faint small pad">No problems' + (any ? "." : " (no language server is running).") + "</p>";
  }

  // ── applying edits (rename, quick fixes, organize imports) ───────────
  async function applyWorkspaceEdit(edit, lang) {
    const id = pid();
    const perUri = {};
    Object.entries(edit.changes || {}).forEach(([u, e]) => (perUri[u] = (perUri[u] || []).concat(e)));
    const rest = (edit.documentChanges || []).filter(dc => {
      if (dc.textDocument) { perUri[dc.textDocument.uri] = (perUri[dc.textDocument.uri] || []).concat(dc.edits || []); return false; }
      return true;
    });
    const disk = {};
    let touched = 0;
    for (const [uri, edits] of Object.entries(perUri)) {
      const path = relPath(id, uri);
      const f = path && P.open.find(x => x.project === id && x.path === path);
      const model = f && Ed().model(Projects.keyOf(f));
      if (model) {
        model.pushEditOperations([], edits.map(e => ({ range: toRange(e.range), text: e.newText })), () => null);
        touched++;
      } else disk[uri] = edits;
    }
    if (Object.keys(disk).length || rest.length) {
      const r = await post(lang || "java", { action: "apply", project: id, edit: { changes: disk, documentChanges: rest } });
      touched += r.changed.length;
      await Projects.refresh(true);
    }
    return touched;
  }
  async function runAction(a, lang) {
    if (!a) return;
    if (!a.command || typeof a.command !== "string") {       // a CodeAction
      if (!a.edit && a.data) a = await call("codeAction/resolve", a, null, 30, lang) || a;
      if (a.edit) await applyWorkspaceEdit(a.edit, lang);
      if (a.command) await runCommand(a.command, lang);
      return;
    }
    await runCommand(a, lang);
  }
  async function runCommand(c, lang) {
    if (c.command === "java.apply.workspaceEdit") return applyWorkspaceEdit(c.arguments[0], "java");
    if (c.command === "java.action.organizeImports") return organizeImports();
    if (/^(_typescript\.|eslint\.)/.test(c.command)) {    // the server runs these and sends the edits back (see "edits")
      await flush(P.file);
      return call("workspace/executeCommand", { command: c.command, arguments: c.arguments || [] }, null, 60, lang);
    }
    toast("That fix isn't available here (" + c.command + ").", true);
  }
  async function organizeImports() {
    const model = Ed().model();
    const d = model && lspDocOf(model);
    if (!d || d.file.jdt) return;
    await flush(d.file);
    const lines = model.getLineCount();
    const acts = await call("textDocument/codeAction", { textDocument: { uri: d.uri }, range: { start: { line: 0, character: 0 }, end: { line: lines - 1, character: 0 } },
      context: { diagnostics: [], only: ["source.organizeImports"], triggerKind: 1 } }, d.project, 30, d.lang) || [];
    if (!acts.length) { toast("The imports are already in order."); return; }
    await runAction(acts[0], d.lang);
  }

  // ── opening things the server points at ──────────────────────────────
  async function openLocation(uri, range) {
    const at = range ? { line: range.start.line + 1, col: range.start.character + 1 } : null;
    if (uri.startsWith("jdt://")) return openClassFile(uri, at);
    const path = relPath(pid(), uri);
    if (!path) { toast("That's outside the project.", true); return; }
    await Projects.openFile(path, false, at);
  }
  // a library class, shown read-only (decompiled when there's no source)
  async function openClassFile(uri, at) {
    const id = pid();
    let f = P.open.find(x => x.project === id && x.jdt === uri);
    if (!f) {
      const text = await call("java/classFileContents", { uri }, id, 60);
      const m = /contents\/[^/]*\/([^/]*)\/([^?]*)\.class/.exec(decodeURIComponent(uri)) || [];
      const name = (m[2] || "Class") + ".class" + (m[1] ? " (" + m[1] + ")" : "");
      f = { project: id, path: "[library] " + name, jdt: uri, text: text || "// No source or decompiled view is available for this class.", readOnly: true, dirty: false };
      P.open.push(f);
    }
    P.file = f;
    P.diffPath = null;
    P.detail = null;
    Projects.renderEditor();
    if (at) Ed().reveal(at.line, at.col);
  }

  // ── Find usages (Alt+F7), shown in the Usages panel ──────────────────
  async function findUsages() {
    const ed = Ed().editor, model = ed.getModel(), d = model && lspDocOf(model);
    if (!d) return;
    const pos = ed.getPosition(), word = model.getWordAtPosition(pos);
    $("#usagesPane").innerHTML = '<p class="faint small pad"><span class="spinner"></span> Finding usages…</p>';
    Projects.showPanel("usages");
    if (!d.file.jdt) await flush(d.file);
    const refs = await call("textDocument/references", { textDocument: { uri: d.uri }, position: toPos(pos), context: { includeDeclaration: false } }, null, 60, d.lang) || [];
    const id = pid(), byFile = {};
    refs.forEach(r => (byFile[r.uri] = byFile[r.uri] || []).push(r));
    const texts = {};
    await Promise.all(Object.keys(byFile).slice(0, 40).map(async uri => {
      const path = relPath(id, uri);
      if (!path) return;
      const open = P.open.find(x => x.project === id && x.path === path);
      texts[uri] = (open ? Ed().value(Projects.keyOf(open)) : (await api("GET", "projects/" + id + "/file?path=" + encodeURIComponent(path)).catch(() => ({}))).text || "").split("\n");
    }));
    J.usages = refs;
    $("#usagesPane").innerHTML = '<div class="ushead">' + refs.length + " usage" + (refs.length === 1 ? "" : "s") + " of <b>" + esc(word ? word.word : "this") + "</b> in " +
      Object.keys(byFile).length + " file" + (Object.keys(byFile).length === 1 ? "" : "s") + "</div>" +
      Object.entries(byFile).map(([uri, list]) => {
        const path = relPath(id, uri) || uri;
        return '<div class="ufile">' + icon("file") + "<b>" + esc(path.split("/").pop()) + '</b><span class="faint tiny">' + esc(path) + "</span></div>" +
          list.sort((a, b) => a.range.start.line - b.range.start.line).map(r => {
            const line = ((texts[uri] || [])[r.range.start.line] || "");
            const a = r.range.start.character, b = r.range.end.line === r.range.start.line ? r.range.end.character : line.length;
            return '<div class="urow" data-uri="' + esc(uri) + '" data-line="' + r.range.start.line + '" data-col="' + a + '"><span class="faint mono tiny">' + (r.range.start.line + 1) +
              '</span><code>' + esc(line.slice(0, a).trimStart()) + "<mark>" + esc(line.slice(a, b)) + "</mark>" + esc(line.slice(b)) + "</code></div>";
          }).join("");
      }).join("") + (refs.length ? "" : '<p class="faint small pad">No usages found.</p>');
  }

  // ── Go to type (Ctrl+O, like NetBeans) ───────────────────────────────
  function goToType(lang) {
    const ts = lang === "ts";
    const d = openModal('<h3>Go to ' + (ts ? "type or component" : "type") + '</h3><input id="gtQ" placeholder="' + (ts ? "A type, interface or component, e.g. CartItem or ProductList" : "Class name, e.g. OrderService or OS (capitals)") +
      '" spellcheck="false" autocomplete="off"><div class="gtlist" id="gtList"></div>');
    d.classList.add("wide");
    let t = null, seq = 0;
    const q = $("#gtQ");
    q.focus();
    // classes, interfaces, enums, structs, type parameters; in TypeScript also functions and constants (React components)
    const kinds = ts ? [5, 10, 11, 12, 13, 14, 23, 26] : [5, 10, 11, 23, 26];
    q.oninput = () => {
      clearTimeout(t);
      t = setTimeout(guard(async () => {
        const mine = ++seq, query = q.value.trim();
        if (!query) { $("#gtList").innerHTML = ""; return; }
        const res = await call("workspace/symbol", { query }, null, 30, lang) || [];
        if (mine !== seq) return;
        const types = res.filter(s => kinds.includes(s.kind) && (!ts || relPath(pid(), s.location.uri) && !/\/node_modules\//.test(s.location.uri))).slice(0, 60);
        $("#gtList").innerHTML = types.map((s, i) => '<button class="gtrow" data-i="' + i + '">' + icon([12, 13, 14].includes(s.kind) ? "dot" : "code") + "<b>" + esc(s.name) + '</b><span class="faint small">' +
          esc(s.containerName || (ts ? relPath(pid(), s.location.uri) : "") || "") + (s.location.uri.startsWith("jdt://") ? " · library" : "") + "</span></button>").join("") || '<p class="faint small">Nothing matches.</p>';
        $$("#gtList .gtrow").forEach(b => (b.onclick = guard(async () => { closeModal(); const s = types[+b.dataset.i]; await openLocation(s.location.uri, s.location.range); })));
      }), 180);
    };
    q.onkeydown = e => { if (e.key === "Enter") { const b = $("#gtList .gtrow"); if (b) b.click(); } };
  }

  // ── the outline (like NetBeans' Navigator) ───────────────────────────
  const KIND_ICON = { 5: "code", 10: "code", 11: "code", 23: "code", 6: "play", 9: "play", 12: "play", 8: "dot", 7: "dot", 13: "dot", 14: "dot", 22: "dot" };
  async function renderOutline() {
    const box = $("#outline");
    const f = P.file;
    const lang = langOf(f);
    if (!f || f.project !== pid() || !lang) { box.innerHTML = '<p class="faint small pad">Open a Java, TypeScript or JavaScript file to see its classes, functions and fields.</p>'; return; }
    if (!running(null, lang)) { box.innerHTML = '<p class="faint small pad">Waiting for the ' + LABEL[lang] + " language server…</p>"; return; }
    const model = Ed().model(Projects.keyOf(f));
    const d = model && lspDocOf(model);
    if (!d) return;
    let syms = [];
    try { syms = await call("textDocument/documentSymbol", { textDocument: { uri: d.uri } }, null, 30, lang) || []; } catch (_) { /* not ready yet */ }
    const row = (s, depth) => '<div class="orow" style="padding-left:' + (6 + depth * 14) + 'px" data-line="' + ((s.selectionRange || s.range || s.location.range).start.line + 1) + '">' +
      icon(KIND_ICON[s.kind] || "dot") + '<span class="k' + s.kind + '">' + esc(s.name) + '</span><span class="faint tiny">' + esc(s.detail || "") + "</span></div>" +
      (s.children || []).map(c => row(c, depth + 1)).join("");
    box.innerHTML = syms.length ? syms.map(s => row(s, 0)).join("") : '<p class="faint small pad">Nothing to show.</p>';
  }

  // ── Run / Debug links on tests and main methods ──────────────────────
  function javaLenses(model) {
    const text = model.getValue(), lines = text.split("\n");
    const pkg = (/^\s*package\s+([\w.]+)\s*;/m.exec(text) || [])[1];
    const out = [];
    let cls = null, clsLine = -1, tests = 0;
    for (let i = 0; i < lines.length; i++) {
      const c = /^\s*(?:public\s+|abstract\s+|final\s+)*(?:class|record)\s+(\w+)/.exec(lines[i]);
      if (c && !cls) { cls = c[1]; clsLine = i; }
      if (/^\s*@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b/.test(lines[i])) {
        for (let k = i + 1; k < Math.min(lines.length, i + 8); k++) {
          const m = /^\s*(?:(?:public|protected|private|static|final|synchronized)\s+)*(?:<[^>]+>\s*)?[\w<>[\],.? ]+\s+(\w+)\s*\(/.exec(lines[k]);
          if (m && !/^\s*@/.test(lines[k])) { tests++; out.push({ line: k + 1, kind: "test", method: m[1] }); break; }
        }
      }
      if (/\bpublic\s+static\s+void\s+main\s*\(/.test(lines[i])) out.push({ line: i + 1, kind: "main" });
    }
    if (!cls) return [];
    const full = (pkg ? pkg + "." : "") + cls;
    const lenses = [];
    if (tests && clsLine >= 0) lenses.push({ line: clsLine + 1, items: [["▶ Run all " + tests + " tests", "test", { cls: full }], ["Debug tests", "debugtest", { cls: full }]] });
    out.forEach(o => {
      if (o.kind === "test") lenses.push({ line: o.line, items: [["▶ Run test", "test", { cls: full, method: o.method }], ["Debug test", "debugtest", { cls: full, method: o.method }]] });
      else lenses.push({ line: o.line, items: [["▶ Run", "run", { cls: full }], ["Debug", "debug", { cls: full }]] });
    });
    return lenses;
  }
  // Vitest / Jest: it("…") and test("…") (describe blocks run all their tests)
  function tsLenses(model, path) {
    if (!/\.(test|spec)\.[cm]?[jt]sx?$/i.test(path) && !/(^|\/)__tests__\//.test(path)) return [];
    const lines = model.getValue().split("\n"), lenses = [];
    let n = 0;
    lines.forEach((l, i) => {
      const m = /^\s*(it|test|describe)(?:\.(?:only|concurrent))?\s*\(\s*(['"`])((?:\\.|(?!\2).)*)\2/.exec(l);
      if (!m) return;
      if (m[1] !== "describe") n++;
      lenses.push({ line: i + 1, items: [[m[1] === "describe" ? "▶ Run these tests" : "▶ Run test", "tstest", { path, name: m[3].replace(/\\(.)/g, "$1") }]] });
    });
    if (n) lenses.unshift({ line: 1, items: [["▶ Run all " + n + " tests in this file", "tstest", { path }]] });
    return lenses;
  }
  async function runTest(args) {
    const r = await api("POST", "java", { action: "test", project: pid(), cls: args.cls, method: args.method || "" });
    await Projects.runTests(r.command);
  }
  async function runTsTest(args) {
    const r = await post("ts", { action: "test", project: pid(), path: args.path, name: args.name || "" });
    await Projects.runTests(r.command);
  }
  async function runMain(args) {
    const kind = P.kind || { tasks: [] };
    const run = kind.tasks.find(t => t.name === "Run");
    const cmd = run && /exec:java|exec:exec/.test(run.command) ? run.command.replace(/\s-Dexec\.mainClass=('[^']*'|"[^"]*"|\S+)/, "") + " -Dexec.mainClass=" + args.cls
      : run ? run.command : null;
    if (!cmd) { toast("This project has no Run command. Set one in the terminal, e.g. mvn exec:java -Dexec.mainClass=" + args.cls, true); return; }
    Projects.showPanel("term");
    $("#termCmd").value = cmd;
    $("#termRun").click();
  }

  // ── Monaco providers (registered once) ───────────────────────────────
  const CK = () => { const k = M().languages.CompletionItemKind; return [null, k.Text, k.Method, k.Function, k.Constructor, k.Field, k.Variable, k.Class, k.Interface, k.Module, k.Property,
    k.Unit, k.Value, k.Enum, k.Keyword, k.Snippet, k.Color, k.File, k.Reference, k.Folder, k.EnumMember, k.Constant, k.Struct, k.Event, k.Operator, k.TypeParameter]; };
  const md = v => {
    if (!v) return undefined;
    if (typeof v === "string") return { value: v };
    if (Array.isArray(v)) return { value: v.map(x => (typeof x === "string" ? x : "```" + (x.language || "") + "\n" + x.value + "\n```")).join("\n\n") };
    if (v.kind) return { value: v.value };
    if (v.language) return { value: "```" + v.language + "\n" + v.value + "\n```" };
    return undefined;
  };
  function registerProviders() {
    const m = M();
    if (!m || J.registered) return;
    J.registered = true;
    const L = m.languages, sel = ["java", "typescript", "javascript"];
    const doc = model => lspDocOf(model);
    const ok = model => { const d = doc(model); return d && running(d.project, d.lang) ? d : null; };
    const ready = async model => { const d = ok(model); if (d && !d.file.jdt) await flush(d.file); return d; };

    L.registerCompletionItemProvider(sel, {
      triggerCharacters: [".", "@", "#", "*", "\"", "'", "/", "<"],
      async provideCompletionItems(model, position, context) {
        const d = await ready(model);
        if (!d || d.file.jdt) return { suggestions: [] };
        const ch = context && context.triggerCharacter;
        if (ch && (d.lang === "java" ? !".@#*".includes(ch) : ch === "#" || ch === "*")) return { suggestions: [] };
        const key = Ed().keyOfUri(model.uri);
        const r = await call("textDocument/completion", { textDocument: { uri: d.uri }, position: toPos(position),
          context: ch ? { triggerKind: 2, triggerCharacter: ch } : { triggerKind: 1 } }, d.project, 30, d.lang);
        const items = Array.isArray(r) ? r : (r && r.items) || [];
        const word = model.getWordUntilPosition(position);
        const def = new m.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
        const kinds = CK();
        return {
          incomplete: !!(r && r.isIncomplete),
          suggestions: items.slice(0, 2000).map(it => {
            const te = it.textEdit;
            return {
              label: it.labelDetails ? { label: it.label, detail: it.labelDetails.detail, description: it.labelDetails.description } : it.label,
              kind: kinds[it.kind] != null ? kinds[it.kind] : m.languages.CompletionItemKind.Text,
              detail: it.detail, documentation: md(it.documentation), sortText: it.sortText, filterText: it.filterText, preselect: it.preselect,
              insertText: te ? te.newText : it.insertText || it.label,
              insertTextRules: it.insertTextFormat === 2 ? m.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
              range: te ? toRange(te.range || te.insert) : def,
              additionalTextEdits: (it.additionalTextEdits || []).map(e => ({ range: toRange(e.range), text: e.newText })),
              tags: it.deprecated || (it.tags || []).includes(1) ? [m.languages.CompletionItemTag.Deprecated] : undefined,
              commitCharacters: it.commitCharacters, _lsp: it, _project: d.project, _key: key, _lang: d.lang,
            };
          }),
        };
      },
      async resolveCompletionItem(item) {
        if (!item._lsp || !item._lsp.data) return item;
        try {
          const r = await call("completionItem/resolve", item._lsp, item._project, 10, item._lang);
          if (r) {
            item.documentation = md(r.documentation) || item.documentation;
            item.detail = r.detail || item.detail;
            if (r.additionalTextEdits) item.additionalTextEdits = r.additionalTextEdits.map(e => ({ range: toRange(e.range), text: e.newText }));   // auto-imports
          }
        } catch (_) { /* keep it unresolved */ }
        return item;
      },
    });
    L.registerHoverProvider(sel, {
      async provideHover(model, position) {
        const d = await ready(model);
        if (!d) return null;
        const r = await call("textDocument/hover", { textDocument: { uri: d.uri }, position: toPos(position) }, d.project, 30, d.lang);
        if (!r || !r.contents) return null;
        const c = md(r.contents);
        return c && c.value.trim() ? { contents: [c], range: r.range ? toRange(r.range) : undefined } : null;
      },
    });
    L.registerSignatureHelpProvider(sel, {
      signatureHelpTriggerCharacters: ["(", ",", "<"], signatureHelpRetriggerCharacters: [")"],
      async provideSignatureHelp(model, position) {
        const d = await ready(model);
        if (!d || d.file.jdt) return null;
        const r = await call("textDocument/signatureHelp", { textDocument: { uri: d.uri }, position: toPos(position) }, d.project, 30, d.lang);
        if (!r || !(r.signatures || []).length) return null;
        return { value: { activeSignature: r.activeSignature || 0, activeParameter: r.activeParameter || 0,
          signatures: r.signatures.map(s => ({ label: s.label, documentation: md(s.documentation), parameters: (s.parameters || []).map(p => ({ label: p.label, documentation: md(p.documentation) })) })) }, dispose() {} };
      },
    });
    const locs = (r, project) => (Array.isArray(r) ? r : r ? [r] : []).map(l => {
      const uri = l.targetUri || l.uri, range = l.targetSelectionRange || l.range;
      if (uri.startsWith("jdt://")) return { uri: m.Uri.from({ scheme: "jdt", path: "/x", query: uri }), range: toRange(range) };
      const path = relPath(project, uri);
      return path ? { uri: Ed().uriOf(project + "\n" + path), range: toRange(range) } : null;
    }).filter(Boolean);
    for (const [fn, method] of [["registerDefinitionProvider", "textDocument/definition"], ["registerTypeDefinitionProvider", "textDocument/typeDefinition"],
      ["registerImplementationProvider", "textDocument/implementation"]]) {
      L[fn](sel, {
        [fn === "registerDefinitionProvider" ? "provideDefinition" : fn === "registerTypeDefinitionProvider" ? "provideTypeDefinition" : "provideImplementation"]: async (model, position) => {
          const d = await ready(model);
          if (!d) return [];
          return locs(await call(method, { textDocument: { uri: d.uri }, position: toPos(position) }, d.project, 60, d.lang), d.project);
        },
      });
    }
    L.registerDocumentHighlightProvider(sel, {
      async provideDocumentHighlights(model, position) {
        const d = await ready(model);
        if (!d) return [];
        const r = await call("textDocument/documentHighlight", { textDocument: { uri: d.uri }, position: toPos(position) }, d.project, 30, d.lang) || [];
        return r.map(h => ({ range: toRange(h.range), kind: h.kind ? h.kind - 1 : 0 }));
      },
    });
    L.registerDocumentSymbolProvider(sel, {
      async provideDocumentSymbols(model) {
        const d = ok(model);
        if (!d) return [];
        const conv = s => ({ name: s.name, detail: s.detail || "", kind: (s.kind || 1) - 1, tags: [], range: toRange(s.range || s.location.range),
          selectionRange: toRange(s.selectionRange || s.range || s.location.range), children: (s.children || []).map(conv) });
        return (await call("textDocument/documentSymbol", { textDocument: { uri: d.uri } }, d.project, 30, d.lang) || []).map(conv);
      },
    });
    const fmt = async (model, range, options) => {
      const d = await ready(model);
      if (!d || d.file.jdt) return [];
      if (d.lang === "ts" && !range) {          // the project's own Prettier and config, like it formats in the build
        const r = await post("ts", { action: "prettier", project: d.project, path: d.file.path, text: model.getValue() });
        if (r.text != null) return r.text === model.getValue() ? [] : [{ range: model.getFullModelRange(), text: r.text }];
      }
      const opt = { tabSize: options.tabSize, insertSpaces: options.insertSpaces };
      const r = range ? await call("textDocument/rangeFormatting", { textDocument: { uri: d.uri }, range: { start: toPos(range.getStartPosition()), end: toPos(range.getEndPosition()) }, options: opt }, d.project, 30, d.lang)
        : await call("textDocument/formatting", { textDocument: { uri: d.uri }, options: opt }, d.project, 30, d.lang);
      return (r || []).map(e => ({ range: toRange(e.range), text: e.newText }));
    };
    L.registerDocumentFormattingEditProvider(sel, { provideDocumentFormattingEdits: (model, options) => fmt(model, null, options) });
    L.registerDocumentRangeFormattingEditProvider(sel, { provideDocumentRangeFormattingEdits: (model, range, options) => fmt(model, range, options) });
    L.registerRenameProvider(sel, {
      async resolveRenameLocation(model, position) {
        const d = await ready(model);
        if (!d || d.file.jdt) return { rejectReason: "You can't rename here." };
        const r = await call("textDocument/prepareRename", { textDocument: { uri: d.uri }, position: toPos(position) }, d.project, 30, d.lang);
        if (!r) return { rejectReason: "This can't be renamed." };
        const range = toRange(r.range || r);
        return { range, text: r.placeholder || model.getValueInRange(range) };
      },
      async provideRenameEdits(model, position, newName) {
        const d = await ready(model);
        if (!d) return { edits: [] };
        const edit = await call("textDocument/rename", { textDocument: { uri: d.uri }, position: toPos(position), newName }, d.project, 60, d.lang);
        if (edit) {
          const n = await applyWorkspaceEdit(edit, d.lang);
          toast("Renamed to " + newName + " in " + n + " file" + (n === 1 ? "" : "s") + ". Save the open ones to keep it.");
        }
        return { edits: [] };       // already applied above (Monaco on its own can only edit open files)
      },
    });
    L.registerCodeActionProvider(sel, {
      async provideCodeActions(model, range, context) {
        const d = await ready(model);
        if (!d || d.file.jdt) return { actions: [], dispose() {} };
        const ask = async lang => {
          if (!running(d.project, lang)) return [];
          const diags = diagsOf(d.project, d.uri, lang).filter(x => x.range.start.line + 1 <= range.endLineNumber && x.range.end.line + 1 >= range.startLineNumber);
          const r = await call("textDocument/codeAction", { textDocument: { uri: d.uri }, range: { start: toPos(range.getStartPosition()), end: toPos(range.getEndPosition()) },
            context: { diagnostics: diags, only: context.only ? [context.only] : undefined, triggerKind: context.trigger === 1 ? 1 : 2 } }, d.project, 30, lang).catch(() => []) || [];
          return r.map(a => ({ a, lang }));
        };
        const all = (await Promise.all((d.lang === "ts" ? ["ts", "eslint"] : [d.lang]).map(ask))).flat();
        return {
          actions: all.map(({ a, lang }) => ({ title: a.title, kind: a.kind || (a.command && typeof a.command === "string" ? "" : "quickfix"), isPreferred: !!a.isPreferred,
            diagnostics: context.markers.filter(mk => mk.owner === "lsp"), command: { id: "va.lsp.action", title: a.title, arguments: [a, lang] } })),
          dispose() {},
        };
      },
    });
    const lensCmd = { test: "va.java.test", debugtest: "va.java.debugtest", run: "va.java.run", debug: "va.java.debug", tstest: "va.ts.test" };
    L.registerCodeLensProvider(sel, {
      provideCodeLenses(model) {
        const d = doc(model);
        if (!d || d.file.jdt) return { lenses: [], dispose() {} };
        const lenses = [];
        (d.lang === "java" ? javaLenses(model) : tsLenses(model, d.file.path)).forEach(l =>
          l.items.forEach(([title, kind, args]) => lenses.push({ range: new m.Range(l.line, 1, l.line, 1), command: { id: lensCmd[kind], title, arguments: [args] } })));
        return { lenses, dispose() {} };
      },
    });
    m.editor.registerCommand("va.lsp.action", (_acc, a, lang) => guard(runAction)(a, lang));
    m.editor.registerCommand("va.java.test", (_acc, a) => guard(runTest)(a));
    m.editor.registerCommand("va.ts.test", (_acc, a) => guard(runTsTest)(a));
    m.editor.registerCommand("va.java.run", (_acc, a) => guard(runMain)(a));
    m.editor.registerCommand("va.java.debugtest", (_acc, a) => guard(() => (window.VADebug ? window.VADebug.debugTest(a) : null))());
    m.editor.registerCommand("va.java.debug", (_acc, a) => guard(() => (window.VADebug ? window.VADebug.debugMain(a) : null))());
    // go to definition into another file or a library class
    m.editor.registerEditorOpener({
      openCodeEditor(_source, resource, sel) {
        const at = sel && (sel.startLineNumber ? { line: sel.startLineNumber, col: sel.startColumn } : sel.lineNumber ? { line: sel.lineNumber, col: sel.column } : null);
        if (resource.scheme === "jdt") { guard(openClassFile)(resource.query, at); return true; }
        const key = Ed().keyOfUri(resource);
        if (!key) return false;
        const [project, ...rest] = key.split("\n");
        if (project !== pid()) return false;
        guard(Projects.openFile)(rest.join("\n"), false, at);
        return true;
      },
    });
    // NetBeans keys, next to the VS Code ones Monaco already has
    const ed = Ed().editor, K = m.KeyMod, C = m.KeyCode;
    const langOnly = fn => () => { const d = doc(ed.getModel()); if (d) fn(d.lang); };
    const trig = id => () => ed.trigger("keyboard", id, {});
    ed.addAction({ id: "va.quickfix", label: "Show fixes (Alt+Enter)", keybindings: [K.Alt | C.Enter], run: trig("editor.action.quickFix") });
    ed.addAction({ id: "va.definition", label: "Go to declaration", keybindings: [K.CtrlCmd | C.KeyB], run: trig("editor.action.revealDefinition") });
    ed.addAction({ id: "va.usages", label: "Find usages", keybindings: [K.Alt | C.F7, K.Shift | C.F12], contextMenuGroupId: "navigation", contextMenuOrder: 1.5, run: langOnly(guard(findUsages)) });
    ed.addAction({ id: "va.rename", label: "Rename…", keybindings: [K.CtrlCmd | C.KeyR], run: trig("editor.action.rename") });
    ed.addAction({ id: "va.imports", label: "Fix imports", keybindings: [K.CtrlCmd | K.Shift | C.KeyI], contextMenuGroupId: "1_modification", run: langOnly(guard(organizeImports)) });
    ed.addAction({ id: "va.gototype", label: "Go to type…", keybindings: [K.CtrlCmd | C.KeyO], run: langOnly(goToType) });
    ed.addAction({ id: "va.format", label: "Format", keybindings: [K.Alt | K.Shift | C.KeyF], run: trig("editor.action.formatDocument") });
  }



  // ── the debugger ─────────────────────────────────────────────────────
  // breakpoints: per project, {path: [{line, condition}]}, kept in this browser
  const D = { session: null, state: "", frames: [], scopes: [], frame: null, thread: null, console: [], bpDecos: new Map(), curDeco: new Map(), keys: null, label: "" };
  const bpKey = id => "va-bp-" + id;
  function bps(id) {
    id = id || pid();
    if (!J.bps) J.bps = {};
    if (!J.bps[id]) { try { J.bps[id] = JSON.parse(localStorage.getItem(bpKey(id)) || "{}"); } catch (_) { J.bps[id] = {}; } }
    return J.bps[id];
  }
  function saveBps(id) { try { localStorage.setItem(bpKey(id || pid()), JSON.stringify(bps(id))); } catch (_) { /* private window */ } }
  function drawBreakpoints(f) {
    if (!M() || !f || f.jdt) return;
    const model = Ed().model(Projects.keyOf(f));
    if (!model) return;
    const lines = (bps(f.project)[f.path] || []);
    const old = D.bpDecos.get(model) || [];
    D.bpDecos.set(model, model.deltaDecorations(old, lines.map(b => ({
      range: new (M().Range)(b.line, 1, b.line, 1),
      options: { glyphMarginClassName: "va-bp" + (b.condition ? " cond" : ""), glyphMarginHoverMessage: { value: b.condition ? "Breakpoint if `" + b.condition + "`" : "Breakpoint" },
        stickiness: M().editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges },
    }))));
  }
  // edits move breakpoints with their lines
  function trackBreakpoints(f) {
    const model = f && Ed().model(Projects.keyOf(f));
    const ids = model && D.bpDecos.get(model);
    if (!ids || !ids.length) return;
    const list = bps(f.project)[f.path] || [];
    const lines = ids.map(id => (model.getDecorationRange(id) || {}).startLineNumber).filter(Boolean);
    if (lines.length === list.length && lines.some((l, i) => l !== list[i].line)) {
      bps(f.project)[f.path] = list.map((b, i) => Object.assign({}, b, { line: lines[i] }));
      saveBps(f.project);
    }
  }
  async function toggleBreakpoint(f, line, condition) {
    if (!f || f.jdt || !isJavaPath(f.path)) return;
    const map = bps(f.project);
    const list = map[f.path] = (map[f.path] || []).slice();
    const i = list.findIndex(b => b.line === line);
    if (condition !== undefined) { if (i >= 0) list[i].condition = condition; else list.push({ line, condition }); }
    else if (i >= 0) list.splice(i, 1);
    else list.push({ line });
    list.sort((a, b) => a.line - b.line);
    if (!list.length) delete map[f.path];
    saveBps(f.project);
    drawBreakpoints(f);
    renderDebug();
    if (D.session && D.state !== "ended") {
      await api("POST", "java", { action: "debug", op: "breakpoints", project: f.project, path: f.path, lines: map[f.path] || [] }).catch(() => {});
    }
  }
  function conditionDialog(f, line) {
    const cur = (bps(f.project)[f.path] || []).find(b => b.line === line);
    const d = openModal('<h3>Breakpoint condition</h3><p class="faint small">Stop on line ' + line + " only when this Java expression is true, e.g. <code>items &gt; 10</code>. Leave it empty to always stop.</p>" +
      '<input id="bpCond" spellcheck="false" value="' + esc(cur ? cur.condition || "" : "") + '"><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Save</button></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="ok"]').onclick = guard(async () => { const c = $("#bpCond").value.trim(); closeModal(); await toggleBreakpoint(f, line, c); });
    $("#bpCond").focus();
  }
  const relBps = id => { const out = {}; Object.entries(bps(id)).forEach(([p, l]) => { if (l.length) out[p] = l; }); return out; };

  async function debugMain(args) {
    D.console = [{ cat: "console", text: "Starting " + args.cls + " in the debugger…\n" }];
    D.state = "starting"; D.label = args.cls; D.frames = []; D.scopes = [];
    D.session = pid();
    Projects.showPanel("debug");
    renderDebug();
    await api("POST", "java", { action: "debug", op: "launch", project: pid(), cls: args.cls, breakpoints: relBps(pid()) });
  }
  async function debugTest(args) {
    D.console = [{ cat: "console", text: "Running " + (args.method ? args.cls.split(".").pop() + "." + args.method : args.cls) + " with the debugger. The build tool compiles first, then the test waits for the debugger…\n" }];
    D.state = "starting"; D.label = args.cls.split(".").pop() + (args.method ? "." + args.method : ""); D.frames = []; D.scopes = [];
    D.session = pid();
    renderDebug();
    const r = await api("POST", "java", { action: "debug", op: "test", project: pid(), cls: args.cls, method: args.method || "", breakpoints: relBps(pid()) });
    P.term[pid()] = (P.term[pid()] || "") + (P.term[pid()] ? "\n" : "") + "$ " + r.command + "\n";
    P.running[pid()] = r.run;
    Projects.showPanel("debug");
  }
  async function debugProject() {
    const r = await call("workspace/executeCommand", { command: "vscode.java.resolveMainClass", arguments: [rootUri()] }, null, 60) || [];
    if (!r.length) { toast("No class with a main method was found to debug. Use Debug test on a test instead.", true); return; }
    if (r.length === 1) return debugMain({ cls: r[0].mainClass });
    const d = openModal("<h3>Debug which program?</h3><div class=\"gtlist\">" + r.map((m, i) => '<button class="gtrow" data-i="' + i + '">' + icon("play") + "<b>" + esc(m.mainClass) + "</b></button>").join("") + "</div>");
    d.querySelectorAll("[data-i]").forEach(b => (b.onclick = guard(async () => { closeModal(); await debugMain({ cls: r[+b.dataset.i].mainClass }); })));
  }
  async function control(what) {
    if (!D.session) return;
    await api("POST", "java", { action: "debug", op: "control", project: D.session, what, thread: D.thread });
  }
  function clearCurrent() {
    D.curDeco.forEach((ids, model) => { if (!model.isDisposed()) model.deltaDecorations(ids, []); });
    D.curDeco.clear();
  }
  async function showFrame(fr) {
    D.frame = fr;
    clearCurrent();
    if (fr.path) await Projects.openFile(fr.path, false, { line: fr.line, col: 1 });
    else if (fr.ref) {
      const id = D.session;
      let f = P.open.find(x => x.project === id && x.dbgRef === fr.ref);
      if (!f) {
        const r = await api("POST", "java", { action: "debug", op: "source", project: id, ref: fr.ref });
        f = { project: id, path: "[library] " + fr.source, jdt: "debug:" + fr.ref, dbgRef: fr.ref, text: r.content, readOnly: true, dirty: false };
        P.open.push(f);
      }
      P.file = f; P.diffPath = null; P.detail = null;
      Projects.renderEditor();
      Ed().reveal(fr.line, 1);
    } else { renderDebug(); return; }
    const model = Ed().model();
    if (model) D.curDeco.set(model, model.deltaDecorations([], [{ range: new (M().Range)(fr.line, 1, fr.line, 1),
      options: { isWholeLine: true, className: "va-curline", glyphMarginClassName: "va-curarrow" } }]));
    renderDebug();
  }
  const varRow = (v, depth, path) => '<div class="vrow" style="padding-left:' + (6 + depth * 14) + 'px" data-ref="' + v.ref + '" data-path="' + esc(path) + '">' +
    (v.ref ? '<span class="vtw">' + icon(D.open && D.open.has(path) ? "chevron-down" : "chevron-right") + "</span>" : '<span class="vtw"></span>') +
    '<span class="vname">' + esc(v.name) + '</span><span class="vval" title="' + esc(v.type) + '">' + esc(v.value) + "</span></div>" +
    (v.ref && D.open && D.open.has(path) && D.kids && D.kids[path] ? D.kids[path].map(c => varRow(c, depth + 1, path + "/" + c.name)).join("") : "");
  function renderDebug() {
    const pane = $("#debugPane");
    const tab = $('#panelTabs [data-panel="debug"]');
    if (tab) tab.hidden = !J.isJava[pid()];
    const live = D.session === pid() && D.state && D.state !== "ended";
    const stopped = live && D.state === "stopped";
    if (D.keys) D.keys.set(!!stopped);
    const b = (what, label, key, on, ic) => '<button class="btn small" data-dbg="' + what + '"' + (on ? "" : " disabled") + ' title="' + esc(label + " (" + key + ")") + '">' + icon(ic) + esc(label) + "</button>";
    const bpCount = Object.values(bps()).reduce((n, l) => n + l.length, 0);
    let h = '<div class="dbar">' + (live ? b("continue", "Continue", "F5", stopped, "play") + b("next", "Step over", "F8 / F10", stopped, "chevron-right") +
      b("stepIn", "Step into", "F7 / F11", stopped, "chevron-down") + b("stepOut", "Step out", "Ctrl+F7 / Shift+F11", stopped, "chevron-up") +
      b("pause", "Pause", "", live && !stopped && D.state !== "starting", "stop") + '<button class="btn small danger" data-dbg="stop" title="Stop debugging (Shift+F5)">' + icon("close") + "Stop</button>"
      : '<button class="btn small primary" data-dbg="project" title="Debug the program (its main method)">' + icon("play") + "Debug program</button>" +
        '<span class="faint small">or use the Debug link above a test or main method.</span>') +
      '<span class="dstate">' + (live ? esc(D.state === "stopped" ? "Paused" + (D.reason ? " · " + D.reason : "") : D.state === "starting" ? "Starting…" : "Running") + " · " + esc(D.label) : "") + "</span>" +
      '<span class="faint tiny">' + bpCount + " breakpoint" + (bpCount === 1 ? "" : "s") + ' · click the margin left of a line number, or F9</span></div>';
    const frames = stopped ? D.frames.map(fr => '<div class="frow' + (D.frame && D.frame.id === fr.id ? " on" : "") + (fr.path || fr.ref ? "" : " ext") + '" data-frame="' + fr.id + '"><span>' + esc(fr.name) +
      '</span><span class="faint tiny">' + esc(fr.source) + ":" + fr.line + "</span></div>").join("") : '<p class="faint small pad">' + (live ? "Shown when the program is paused." : "Not debugging.") + "</p>";
    const vars = stopped ? D.scopes.map(sc => '<div class="vscope">' + esc(sc.name) + "</div>" + sc.vars.map(v => varRow(v, 0, sc.name + "/" + v.name)).join("")).join("") || '<p class="faint small pad">No variables here.</p>' : "";
    const cons = D.console.map(c => '<span class="c-' + esc(c.cat) + '">' + esc(c.text) + "</span>").join("");
    h += '<div class="dgrid"><div class="dcol"><div class="dhead">Call stack</div><div class="dlist">' + frames + '</div></div><div class="dcol"><div class="dhead">Variables</div><div class="dlist" id="dVars">' + vars +
      '</div></div><div class="dcol"><div class="dhead">Console</div><pre class="dcons" id="dCons">' + cons + '</pre><input id="dEval" spellcheck="false" placeholder="' +
      (stopped ? "Evaluate a Java expression, e.g. order.getLines().size()" : "Expressions can be evaluated while paused") + '"' + (stopped ? "" : " disabled") + "></div></div>";
    const keepEval = $("#dEval") ? $("#dEval").value : "";
    pane.innerHTML = h;
    if (keepEval) $("#dEval").value = keepEval;
    const c = $("#dCons");
    if (c) c.scrollTop = c.scrollHeight;
  }
  function onDebug(ev) {
    if (ev.project !== D.session) return;
    if (ev.kind === "output") {
      D.console.push({ cat: ev.category, text: ev.text });
      if (D.console.length > 2000) D.console.splice(0, 500);
      const c = $("#dCons");
      if (c) { c.insertAdjacentHTML("beforeend", '<span class="c-' + esc(ev.category) + '">' + esc(ev.text) + "</span>"); c.scrollTop = c.scrollHeight; }
      return;
    }
    if (ev.kind === "state") { D.state = ev.state; if (ev.label) D.label = ev.label; clearCurrent(); renderDebug(); return; }
    if (ev.kind === "stopped") {
      D.state = "stopped"; D.reason = ev.description || ev.reason; D.frames = ev.frames; D.scopes = ev.scopes; D.thread = ev.thread;
      D.open = new Set(); D.kids = {};
      Projects.showPanel("debug");
      const top = ev.frames.find(f => f.path || f.ref) || ev.frames[0];
      if (top) guard(showFrame)(top); else renderDebug();
      return;
    }
    if (ev.kind === "ended") {
      D.state = "ended"; clearCurrent();
      D.console.push({ cat: "console", text: "\nDebugging ended.\n" });
      renderDebug();
    }
  }
  function wireDebug() {
    $("#debugPane").addEventListener("click", guard(async e => {
      const b = e.target.closest("[data-dbg]");
      if (b) {
        const w = b.dataset.dbg;
        if (w === "project") return debugProject();
        await control(w);
        return;
      }
      const fr = e.target.closest("[data-frame]");
      if (fr) {
        const f = D.frames.find(x => String(x.id) === fr.dataset.frame);
        if (!f) return;
        const r = await api("POST", "java", { action: "debug", op: "frame", project: D.session, frame: f.id });
        D.scopes = r.scopes; D.open = new Set(); D.kids = {};
        await showFrame(f);
        return;
      }
      const v = e.target.closest(".vrow");
      if (v && +v.dataset.ref) {
        const path = v.dataset.path;
        if (D.open.has(path)) D.open.delete(path);
        else {
          if (!D.kids[path]) D.kids[path] = (await api("POST", "java", { action: "debug", op: "variables", project: D.session, ref: +v.dataset.ref })).vars;
          D.open.add(path);
        }
        renderDebug();
      }
    }));
    $("#debugPane").addEventListener("keydown", guard(async e => {
      if (e.target.id !== "dEval" || e.key !== "Enter") return;
      const ex = e.target.value.trim();
      if (!ex) return;
      e.target.value = "";
      D.console.push({ cat: "input", text: "> " + ex + "\n" });
      try {
        const r = await api("POST", "java", { action: "debug", op: "evaluate", project: D.session, expression: ex, frame: D.frame ? D.frame.id : null });
        D.console.push({ cat: "result", text: r.value + (r.type ? "   (" + r.type + ")" : "") + "\n" });
      } catch (err) { D.console.push({ cat: "stderr", text: err.message + "\n" }); }
      renderDebug();
      $("#dEval").focus();
    }));
    // NetBeans and VS Code debug keys, anywhere on the Projects page
    document.addEventListener("keydown", e => {
      if (app.S.view !== "projects" || !D.session || D.state === "ended" || !D.state) return;
      const k = e.key, stopped = D.state === "stopped";
      let what = null;
      if (k === "F5" && !e.shiftKey && !e.ctrlKey && stopped) what = "continue";
      else if (k === "F5" && e.shiftKey) what = "stop";
      else if ((k === "F8" || k === "F10") && !e.ctrlKey && !e.shiftKey && stopped) what = "next";
      else if ((k === "F7" || k === "F11") && !e.ctrlKey && !e.shiftKey && !e.altKey && stopped) what = "stepIn";
      else if (((k === "F7" && e.ctrlKey) || (k === "F11" && e.shiftKey)) && stopped) what = "stepOut";
      if (!what) return;
      e.preventDefault();
      e.stopPropagation();
      guard(control)(what);
    }, true);
  }
  function editorDebugHooks() {
    const m = M(), ed = Ed().editor;
    ed.onMouseDown(e => {
      const t = e.target;
      if (t.type !== m.editor.MouseTargetType.GUTTER_GLYPH_MARGIN) return;
      const f = P.file;
      if (!f || f.jdt || !isJavaPath(f.path)) return;
      if (e.event.rightButton) { conditionDialog(f, t.position.lineNumber); return; }
      guard(toggleBreakpoint)(f, t.position.lineNumber);
    });
    const K = m.KeyMod, C = m.KeyCode;
    ed.addAction({ id: "va.bp", label: "Toggle breakpoint", keybindings: [C.F9, K.CtrlCmd | C.F8], run: () => { const f = P.file; if (f) guard(toggleBreakpoint)(f, ed.getPosition().lineNumber); } });
    ed.addAction({ id: "va.bpcond", label: "Breakpoint with a condition…", contextMenuGroupId: "debug", run: () => { const f = P.file; if (f && !f.jdt) conditionDialog(f, ed.getPosition().lineNumber); } });
    // while paused, F8/F7/F10/F11 step instead of Monaco's own meanings for them
    const stoppedKey = ed.createContextKey("vaDebugStopped", false);
    D.keys = stoppedKey;
    [[C.F8, "next"], [C.F10, "next"], [C.F7, "stepIn"], [C.F11, "stepIn"], [K.CtrlCmd | C.F7, "stepOut"], [K.Shift | C.F11, "stepOut"], [C.F5, "continue"]].forEach(([key, what]) =>
      ed.addCommand(key, () => guard(control)(what), "vaDebugStopped"));
    // hover shows a variable's value while paused
    m.languages.registerHoverProvider("java", {
      async provideHover(model, position) {
        if (D.state !== "stopped" || !D.frame) return null;
        const w = model.getWordAtPosition(position);
        if (!w || /^(public|private|protected|static|final|void|int|double|long|boolean|new|return|if|for|while|class)$/.test(w.word)) return null;
        try {
          const r = await api("POST", "java", { action: "debug", op: "evaluate", project: D.session, expression: w.word, frame: D.frame.id });
          return { range: new m.Range(position.lineNumber, w.startColumn, position.lineNumber, w.endColumn), contents: [{ value: "**" + w.word + "** = `" + r.value + "`" + (r.type ? "  *" + r.type + "*" : "") }] };
        } catch (_) { return null; }
      },
    });
  }
  window.VADebug = { debugMain: a => guard(debugMain)(a), debugTest: a => guard(debugTest)(a) };

  // ── events from the server ───────────────────────────────────────────
  function onEvent(ev) {
    if (ev.type === "debug") { onDebug(ev); return; }
    if (ev.type === "java_setup") {
      const ts = ev.lang === "ts", tools = ts ? J.ts.tools : J.tools;
      const set = v => { if (ts) J.ts.progress = v; else J.progress = v; };
      if (ev.label) set(ev.label + (ev.total ? " " + Math.round(ev.done / ev.total * 100) + "%" : ""));
      if (ev.ok || (ev.finished && !ev.error)) { set(""); toast((ts ? "TypeScript" : "Java") + " support is set up."); guard(refreshStatus)(true); return; }
      if (ev.error) { set(""); if (tools) { tools.error = ev.error; tools.busy = false; } }
      renderBar();
      return;
    }
    if (ev.type !== "java") return;
    const id = ev.project, lang = ev.lang || "java";
    const table = lang === "ts" ? J.ts.info : lang === "eslint" ? J.ts.eslint : J.info;
    if (ev.kind === "status") {
      const info = table[id] = Object.assign(table[id] || {}, { state: ev.state, message: ev.message });
      if (ev.state === "ready" || ev.state === "importing") {
        if (lang === "java") J.progress = ev.state === "ready" ? "" : J.progress;
        if (id === pid()) syncAllOpen();
      }
      if (ev.state === "stopped" || ev.state === "error") {
        const root = info.root_uri || rootUri(id) || "\0";
        [...J.opened].forEach(k => { if (k.startsWith(lang + " " + root)) J.opened.delete(k); });
      }
      if (id === pid()) { renderBar(); if (ev.state === "ready" && P.side === "outline") renderOutline(); }
    } else if (ev.kind === "progress") {
      if (lang === "java") J.progress = ev.done ? "" : [ev.task, ev.sub].filter(Boolean).join(": ").slice(0, 140);
      if (id === pid() && table[id] && table[id].state !== "ready") renderBar();
    } else if (ev.kind === "diagnostics") {
      const all = J.diags[id] = J.diags[id] || {};
      const mine = all[lang] = all[lang] || {};
      if (ev.items.length) mine[ev.uri] = ev.items; else delete mine[ev.uri];
      if (id === pid()) {
        const path = relPath(id, ev.uri);
        const f = path && P.open.find(x => x.project === id && x.path === path);
        const model = f && Ed().model(Projects.keyOf(f));
        if (model) markersFor(id, path, model);
        clearTimeout(J.timers.problems);
        J.timers.problems = setTimeout(() => { renderProblems(); renderBar(); }, 150);
      }
    } else if (ev.kind === "edits") {
      // edits the server made itself (a command it ran): apply them to open files, reload the rest
      ev.changes.filter(c => c.open).forEach(c => {
        const path = relPath(id, c.uri), f = path && P.open.find(x => x.project === id && x.path === path);
        const model = f && Ed().model(Projects.keyOf(f));
        if (model) model.pushEditOperations([], c.edits.map(e => ({ range: toRange(e.range), text: e.newText })), () => null);
      });
      if (ev.changes.some(c => !c.open) && id === pid()) guard(Projects.refresh)(true);
    } else if (ev.kind === "message" && id === pid()) {
      toast(LABEL[lang] + ": " + ev.message, true);
    }
  }

  // ── wiring ───────────────────────────────────────────────────────────
  function wire() {
    $("#javaBar").addEventListener("click", guard(async e => {
      const b = e.target.closest("[data-j]");
      if (!b) return;
      const [lang, a] = b.dataset.j.includes("-") ? b.dataset.j.split("-") : [null, b.dataset.j];
      if (a === "problems") { Projects.showPanel("problems"); return; }
      if (a === "setup") {
        const r = await post(lang, { action: "setup" });
        if (lang === "ts") { J.ts.tools = r.tools; J.ts.progress = "Installing…"; } else { J.tools = r.tools; J.progress = "Downloading…"; }
        renderBar();
      } else if (a === "start" || a === "restart") {
        [...J.opened].forEach(k => { if (k.startsWith(lang + " ") || (lang === "ts" && k.startsWith("eslint "))) J.opened.delete(k); });
        const r = await post(lang, { action: a, project: pid() });
        if (lang === "ts") J.ts.info[pid()] = r.server; else J.info[pid()] = r.server;
        if (lang === "ts") setTimeout(guard(() => refreshStatus(false)), 1500);   // ESLint starts next to it
        renderBar();
      }
    }));
    $("#problemsPane").addEventListener("click", guard(async e => {
      const r = e.target.closest(".prow");
      if (r) await Projects.openFile(r.dataset.path, false, { line: +r.dataset.line, col: +r.dataset.col });
    }));
    $("#usagesPane").addEventListener("click", guard(async e => {
      const r = e.target.closest(".urow");
      if (r) await openLocation(r.dataset.uri, { start: { line: +r.dataset.line, character: +r.dataset.col } });
    }));
    $("#outline").addEventListener("click", e => { const r = e.target.closest(".orow"); if (r) Ed().reveal(+r.dataset.line, 1); });
    const sideTab = $("#outlineTab");
    sideTab.addEventListener("click", () => setTimeout(guard(renderOutline), 0));
    // hooks from projects.js
    P.onOpen = f => syncOpen(f);
    P.onRender = f => {
      if (!f || !M()) return;
      drawBreakpoints(f);
      if (D.frame && D.state === "stopped" && f.path === D.frame.path) {
        const model = Ed().model();
        if (model && !D.curDeco.has(model)) D.curDeco.set(model, model.deltaDecorations([], [{ range: new (M().Range)(D.frame.line, 1, D.frame.line, 1),
          options: { isWholeLine: true, className: "va-curline", glyphMarginClassName: "va-curarrow" } }]));
      }
      if (!f.jdt && langOf(f)) { syncOpen(f); const m = Ed().model(Projects.keyOf(f)); if (m) markersFor(f.project, f.path, m); }
      if (P.side === "outline") { clearTimeout(J.timers.outline); J.timers.outline = setTimeout(guard(renderOutline), 300); }
    };
    P.onSave = f => {
      if (f.jdt || !langOf(f)) return;
      const uri = lspUri(f.project, f.path);
      serversFor(f).filter(l => running(f.project, l) && J.opened.has(l + " " + uri))
        .forEach(lang => post(lang, { action: "sync", project: f.project, op: "save", uri, text: f.text }).catch(() => {}));
    };
    P.onClose = f => {
      if (f.jdt || !langOf(f)) return;
      const uri = lspUri(f.project, f.path);
      serversFor(f).forEach(lang => {
        if (!J.opened.has(lang + " " + uri)) return;
        J.opened.delete(lang + " " + uri);
        post(lang, { action: "sync", project: f.project, op: "close", uri }).catch(() => {});
      });
    };
    Ed().on("change", key => {
      syncChange(key);
      const f = fileByKey(key);
      if (f) { clearTimeout(J.timers["bp" + key]); J.timers["bp" + key] = setTimeout(() => trackBreakpoints(f), 400); }
      if (P.side === "outline" && key === Projects.keyOf(P.file || {})) { clearTimeout(J.timers.outline); J.timers.outline = setTimeout(guard(renderOutline), 800); }
    });
    P.onProject = () => guard(show)();
    P.onPanel = name => { if (name === "debug") renderDebug(); };
    P.onTasks = bar => {
      if (!J.isJava[pid()]) return;
      bar.insertAdjacentHTML("beforeend", '<button class="btn small" data-jdebug="1" title="Debug the program (Ctrl+F5 in NetBeans)">' + icon("play") + "Debug</button>");
      bar.querySelector("[data-jdebug]").onclick = guard(debugProject);
    };
    wireDebug();
    Ed().ready.then(ok => { if (ok) { registerProviders(); editorDebugHooks(); } });
  }

  // called when the Projects page shows a project
  async function show() {
    if (!pid()) return;
    await refreshStatus(true);
    renderDebug();
  }

  wire();
  return { show, onEvent, state: J, lspUri, relPath, call, openLocation, running, applyWorkspaceEdit };
};
