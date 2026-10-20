// Projects: a code workspace (files, editor, Git, terminal) and the project side of chats.
// app.js calls VAProjects(app) once with the helpers it shares.
window.VAProjects = function (app) {
  "use strict";
  const { api, toast, guard, esc, openModal, closeModal, $, $$ } = app;
  const P = {
    list: [], git: true, cur: null, files: [], status: null, openDirs: new Set(), file: null,
    side: "files", term: {}, running: {}, unchecked: new Set(), diffPath: null, defaultParent: "",
  };
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { /* private window */ } return null; };

  // ── diffs ────────────────────────────────────────────────────────────
  function renderDiff(text, opts) {
    if (!text || !text.trim()) return '<div class="diff empty faint small">No changes.</div>';
    const max = (opts && opts.max) || 4000;
    const lines = text.replace(/\n$/, "").split("\n");
    let oldN = 0, newN = 0;
    const rows = [];
    for (const l of lines.slice(0, max)) {
      let cls = "ctx", a = "", b = "";
      if (l.startsWith("@@")) {
        const m = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(l);
        if (m) { oldN = +m[1]; newN = +m[2]; }
        cls = "hunk";
      } else if (l.startsWith("diff --git") || l.startsWith("index ")) {
        continue;
      } else if (l.startsWith("+++") || l.startsWith("---") ||
                 l.startsWith("new file") || l.startsWith("deleted file") || l.startsWith("similarity") ||
                 l.startsWith("rename ") || l.startsWith("old mode") || l.startsWith("new mode") || l.startsWith("Binary")) {
        cls = "meta";
      } else if (l.startsWith("+")) { cls = "add"; b = newN++; }
      else if (l.startsWith("-")) { cls = "del"; a = oldN++; }
      else if (l.startsWith("\\")) { cls = "meta"; }
      else { a = oldN++; b = newN++; }
      rows.push('<div class="dl ' + cls + '"><i>' + a + "</i><i>" + b + "</i><span>" + esc(l) + "</span></div>");
    }
    if (lines.length > max) rows.push('<div class="dl meta"><i></i><i></i><span>… ' + (lines.length - max) + " more lines</span></div>");
    return '<div class="diff">' + rows.join("") + "</div>";
  }

  // ── chat side: approvals and tool cards ─────────────────────────────
  // What a confirm dialog shows for a project tool call. Returns null for other tools.
  function confirmView(ev) {
    const pv = ev.preview;
    if (!pv) return null;
    const a = ev.call.arguments || {};
    if (pv.kind === "diff") {
      const verb = pv.action === "create" ? "Create" : pv.action === "delete" ? "Delete" : "Change";
      return {
        title: verb + " " + pv.path + "?",
        body: '<p class="dim small">The AI wants to ' + (pv.action === "create" ? "create this file" : pv.action === "delete" ?
          "delete this file" : "make this change") + ' <span class="diffstat"><b class="plus">+' + pv.added + '</b> <b class="minus">−' +
          pv.removed + "</b></span></p>" + renderDiff(pv.diff),
        ok: pv.action === "delete" ? "Delete file" : "Apply change", allowAll: "Allow all edits in this chat", note: true, wide: true,
      };
    }
    if (pv.kind === "command") {
      return {
        title: "Run a command?",
        body: '<p class="dim small">In <code>' + esc(pv.cwd) + "</code></p><pre class=\"cmd\">$ " + esc(pv.command) + "</pre>" +
          (pv.prefix ? '<label class="check"><input type="checkbox" id="allowPrefix" value="' + esc(pv.prefix) + '"> Always allow <code>' +
            esc(pv.prefix) + "</code> in this project</label>" : ""),
        ok: "Run", allowAll: null, note: true,
      };
    }
    if (pv.kind === "push") {
      const n = pv.commits.length;
      return {
        title: "Push to " + pv.remote + "/" + pv.branch + "?",
        body: '<p class="dim small">' + (pv.new_branch ? "This creates the branch <b>" + esc(pv.branch) + "</b> on " + esc(pv.remote) + ". " : "") +
          n + " commit" + (n === 1 ? "" : "s") + " will be sent. Never forced.</p>" +
          '<div class="commits">' + pv.commits.map(c => '<div><code>' + esc(c.hash) + "</code> " + esc(c.subject) +
            ' <span class="faint">' + esc(c.when) + "</span></div>").join("") + "</div>",
        ok: "Push", allowAll: null, note: true,
      };
    }
    if (pv.kind === "commit") {
      return {
        title: "Commit these changes?",
        body: '<pre class="cmd">' + esc(pv.message) + '</pre><div class="commits">' + pv.files.map(f =>
          "<div>" + esc(f.status) + " · <code>" + esc(f.path) + "</code></div>").join("") + "</div>",
        ok: "Commit", allowAll: "Allow all in this chat", note: true,
      };
    }
    if (pv.kind === "pull") {
      return { title: "Pull from the remote?", body: '<p class="dim small">' + (pv.mode === "rebase" ? "Rebase your commits on top of the remote's." :
        pv.mode === "merge" ? "Merge the remote's commits into your branch." : "Fast-forward only: nothing of yours is changed.") + "</p>",
      ok: "Pull", allowAll: "Allow all in this chat", note: true };
    }
    if (pv.kind === "branch") {
      return { title: (pv.create ? "Create branch " : "Switch to branch ") + (a.name || "") + "?", body: "",
        ok: pv.create ? "Create and switch" : "Switch", allowAll: "Allow all in this chat", note: true };
    }
    return null;
  }

  // Extra content under a project tool card in the chat: the diff, and Undo.
  function toolExtra(m) {
    let h = "";
    if (m.diff) {
      h += '<details class="tdiff"' + (m.status === "running" ? " open" : "") + "><summary>" + esc(m.path || "diff") +
        " " + diffStat(m.diff) + "</summary>" + renderDiff(m.diff, { max: 600 }) + "</details>";
    }
    if (m.change && m.status === "done") h += '<button class="btn small undo" data-undo="' + esc(m.change) + '">Undo this change</button>';
    return h;
  }
  function diffStat(d) {
    let a = 0, r = 0;
    for (const l of d.split("\n")) {
      if (l.startsWith("+") && !l.startsWith("+++")) a++;
      else if (l.startsWith("-") && !l.startsWith("---")) r++;
    }
    return '<span class="diffstat"><b class="plus">+' + a + '</b> <b class="minus">−' + r + "</b></span>";
  }
  async function undo(projectId, change) {
    const r = await api("POST", "projects/" + projectId + "/undo", { change });
    toast("Undone: " + r.path);
    if (app.S.view === "projects" && P.cur === projectId) refresh(true);
  }

  // choose the project a chat works on
  async function pickForChat() {
    await load();
    const c = app.S.chat;
    const cur = (c && c.project) || "";
    const d = openModal("<h3>Project for this chat</h3><p class=\"faint small\">The AI can read the project's files, propose changes " +
      "(you approve each one), run commands you approve, and use Git.</p><div class=\"opts\">" +
      [["", "None"]].concat(P.list.map(p => [p.id, p.name + " · " + p.path])).map(([v, l]) =>
        '<label class="check"><input type="radio" name="proj" value="' + esc(v) + '"' + (v === cur ? " checked" : "") + "> " + esc(l) + "</label>").join("") +
      '</div><div class="row between"><button class="btn" data-a="add">Add a project…</button><span><button class="btn" data-a="cancel">Cancel</button> ' +
      '<button class="btn primary" data-a="done">Done</button></span></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="add"]').onclick = () => { closeModal(); app.showView("projects"); addDialog("open"); };
    d.querySelector('[data-a="done"]').onclick = guard(async () => {
      const v = (d.querySelector("input[name=proj]:checked") || {}).value || "";
      closeModal();
      await app.patchChat({ project: v });
      app.renderChat();
      const p = P.list.find(x => x.id === v);
      toast(p ? "This chat now works on " + p.name + ". Ask it to explain, fix or build something." : "No project for this chat.");
    });
  }
  function projectName(id) { const p = P.list.find(x => x.id === id); return p ? p.name : ""; }

  // ── projects view ────────────────────────────────────────────────────
  async function load() {
    const r = await api("GET", "projects");
    P.list = r.projects;
    P.git = r.git;
    P.defaultParent = r.default_parent;
    if (!P.list.some(p => p.id === P.cur)) {
      const last = store("va-project");
      P.cur = (P.list.find(p => p.id === last) || P.list[0] || {}).id || null;
    }
    $("#projPick").innerHTML = P.list.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + "</option>").join("");
    if (P.cur) $("#projPick").value = P.cur;
  }

  async function show() {
    await load();
    const has = !!P.cur;
    $("#projEmpty").hidden = has;
    $("#projWs").hidden = !has;
    $("#projPicker").hidden = $("#projSettings").hidden = $("#projChat").hidden = !has;
    $("#gitMissing").hidden = P.git;
    if (has) await openProject(P.cur, true);
    else $("#projTitle").textContent = "Projects";
  }

  function cur() { return P.list.find(p => p.id === P.cur); }

  async function openProject(id, keepFile) {
    if (P.file && P.file.dirty && P.file.project !== id && !confirm("Discard unsaved changes to " + P.file.path + "?")) {
      $("#projPick").value = P.cur;
      return;
    }
    if (P.cur !== id) { P.file = null; P.diffPath = null; P.openDirs = new Set(); P.unchecked = new Set(); }
    P.cur = id;
    store("va-project", id);
    const p = cur();
    $("#projTitle").textContent = p ? p.name : "Projects";
    $("#projTitle").title = p ? p.path : "";
    $("#projPick").value = id;
    renderTerm();
    if (p && !p.exists) {
      $("#tree").innerHTML = '<p class="faint small">The folder ' + esc(p.path) + " isn't there any more. Remove the project in Project settings.</p>";
      $("#gitBar").innerHTML = "";
      return;
    }
    await refresh(keepFile);
  }

  async function refresh(reloadFile) {
    const id = P.cur;
    if (!id) return;
    const [f, g] = await Promise.all([api("GET", "projects/" + id + "/files"), api("GET", "projects/" + id + "/git").catch(e => ({ error: e.message }))]);
    if (id !== P.cur) return;
    P.files = f.files;
    P.status = g;
    renderGitBar();
    renderTree();
    renderChanges();
    renderHistory();
    if (reloadFile && P.file && !P.file.dirty && P.file.project === id) {
      if (P.files.includes(P.file.path)) await openFile(P.file.path, true);
      else { P.file = null; renderEditor(); }
    } else if (!P.file || P.file.project !== id) renderEditor();
    if (P.diffPath) showDiff(P.diffPath);
  }

  // git bar
  function renderGitBar() {
    const g = P.status || {};
    const bar = $("#gitBar");
    if (g.no_git) { bar.innerHTML = '<span class="faint small">Git isn\'t installed, so Git features are off.</span>'; return; }
    if (g.error) { bar.innerHTML = '<span class="bad small">' + esc(g.error) + "</span>"; return; }
    if (!g.is_repo) {
      bar.innerHTML = '<span class="faint small">This folder isn\'t a Git repository.</span><button class="btn small" data-g="init">Initialize Git</button>';
      return;
    }
    const sync = g.upstream ? '<span class="ab" title="Commits to push ↑ and to pull ↓ (as of the last fetch)">↑' + g.ahead + " ↓" + g.behind + "</span>"
      : '<span class="faint small">not on the remote yet</span>';
    let h = '<button class="btn small branch" data-g="branch" title="Switch or create a branch"><svg viewBox="0 0 16 16"><path d="M5 2.5v11M5 10.5c0-3 6-2 6-5.5M11 2.5v3"/><circle cx="5" cy="2.5" r="1.3"/><circle cx="5" cy="13.5" r="1.3"/><circle cx="11" cy="4" r="1.3"/></svg>' +
      esc(g.branch || "?") + " ▾</button>" + sync +
      '<span class="gb-acts"><button class="btn small" data-g="fetch" title="Check the remote for new commits">Fetch</button>' +
      '<button class="btn small" data-g="pull" title="Get new commits (fast-forward only)">Pull' + (g.behind ? " ↓" + g.behind : "") + "</button>" +
      '<button class="btn small' + (g.ahead || (!g.upstream && (g.remotes || []).length) ? " primary" : "") + '" data-g="push" title="Send your commits (never forced)">Push' +
      (g.ahead ? " ↑" + g.ahead : "") + "</button></span>";
    if (g.state) {
      const left = (g.files || []).filter(f => f.conflict).length;
      h += '<div class="gb-state">A ' + esc(g.state) + " is in progress" + (left ? ": " + left + " file" + (left === 1 ? " has" : "s have") + " conflicts. Fix them (or ask the AI to), then Continue." : ". Continue to finish it.") +
        ' <button class="btn small primary" data-g="continue">Continue</button> <button class="btn small danger" data-g="abort">Abort</button></div>';
    }
    bar.innerHTML = h;
  }

  async function gitOp(op, extra) {
    const id = P.cur;
    const labels = { fetch: "Fetching…", pull: "Pulling…", push: "Pushing…", init: "Setting up Git…", continue: "Finishing…", abort: "Aborting…" };
    if (labels[op]) toast(labels[op]);
    try {
      const r = await api("POST", "projects/" + id + "/git", Object.assign({ op }, extra || {}));
      toast(r.output ? r.output.split("\n")[0] : { fetch: "Up to date with the remote.", pull: "Pulled.", init: "Git is set up.",
        continue: "Done.", abort: "Aborted; back to how it was." }[op] || "Done.");
      return r;
    } finally {
      await refresh(true);
    }
  }

  async function pull() {
    try {
      await gitOp("pull", { mode: "ff" });
    } catch (e) {
      if (/both have new commits|diverg/i.test(e.message)) {
        const d = openModal("<h3>Your branch and the remote have both changed</h3><p class=\"dim small\">Pick how to combine them. " +
          "<b>Rebase</b> puts your commits on top of the remote's (a straight history). <b>Merge</b> adds a merge commit. If some lines " +
          "conflict, you'll fix them and press Continue.</p><div class=\"row end\"><button class=\"btn\" data-a=\"cancel\">Cancel</button>" +
          '<button class="btn" data-a="merge">Merge</button><button class="btn primary" data-a="rebase">Rebase</button></div>');
        d.querySelector('[data-a="cancel"]').onclick = closeModal;
        ["merge", "rebase"].forEach(m => (d.querySelector('[data-a="' + m + '"]').onclick = guard(async () => { closeModal(); await gitOp("pull", { mode: m }); })));
        return;
      }
      throw e;
    }
  }

  async function push() {
    const pv = await api("GET", "projects/" + P.cur + "/push_preview");
    if (pv.behind) { toast("The remote has " + pv.behind + " new commit" + (pv.behind === 1 ? "" : "s") + ". Pull first, then push.", true); return; }
    if (!pv.commits.length && !pv.new_branch) { toast("Nothing to push: the remote has all your commits."); return; }
    const n = pv.commits.length;
    const d = openModal("<h3>Push to " + esc(pv.remote) + "/" + esc(pv.branch) + "?</h3><p class=\"dim small\">" +
      (pv.new_branch ? "This creates the branch on the remote. " : "") + n + " commit" + (n === 1 ? "" : "s") + ":</p><div class=\"commits\">" +
      pv.commits.map(c => "<div><code>" + esc(c.hash) + "</code> " + esc(c.subject) + ' <span class="faint">' + esc(c.when) + "</span></div>").join("") +
      '</div><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="push">Push</button></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="push"]').onclick = guard(async () => { closeModal(); await gitOp("push"); });
  }

  function branchMenu() {
    const g = P.status || {};
    const d = openModal("<h3>Branches</h3><div class=\"opts branches\">" + (g.branches || []).map(b =>
      '<button class="btn block-l' + (b === g.branch ? " on" : "") + '" data-b="' + esc(b) + '">' + (b === g.branch ? "● " : "") + esc(b) + "</button>").join("") +
      '</div><label>New branch from here<span class="row tight"><input id="newBranch" placeholder="e.g. fix-login-bug" spellcheck="false">' +
      '<button class="btn primary" data-a="create">Create</button></span></label><div class="row end"><button class="btn" data-a="cancel">Close</button></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelectorAll("[data-b]").forEach(b => (b.onclick = guard(async () => {
      if (b.dataset.b === g.branch) return;
      closeModal();
      await gitOp("switch", { name: b.dataset.b });
    })));
    const create = guard(async () => {
      const name = $("#newBranch").value.trim();
      if (!name) return;
      closeModal();
      await gitOp("switch", { name, create: true });
    });
    d.querySelector('[data-a="create"]').onclick = create;
    $("#newBranch").onkeydown = e => { if (e.key === "Enter") create(); };
  }

  // file tree
  function statusMap() {
    const m = {};
    ((P.status && P.status.files) || []).forEach(f => (m[f.path] = f));
    return m;
  }
  const CODE_CLASS = { "?": "new", A: "new", M: "mod", D: "del", R: "mod", U: "bad" };
  function renderTree() {
    const q = $("#fileFilter").value.trim().toLowerCase();
    const sm = statusMap();
    const badge = p => sm[p] ? '<b class="st ' + (CODE_CLASS[sm[p].code] || "mod") + '">' + (sm[p].code === "?" ? "U" : esc(sm[p].code)) + "</b>" : "";
    const fileRow = (p, label, depth) => '<div class="tf' + (P.file && P.file.path === p ? " active" : "") + '" data-file="' + esc(p) +
      '" style="padding-left:' + (8 + depth * 14) + 'px" title="' + esc(p) + '"><span>' + esc(label) + "</span>" + badge(p) + "</div>";
    if (q) {
      const hits = P.files.filter(p => p.toLowerCase().includes(q)).slice(0, 400);
      $("#tree").innerHTML = hits.map(p => fileRow(p, p, 0)).join("") || '<p class="faint small">No files match.</p>';
      return;
    }
    const root = { dirs: {}, files: [] };
    for (const p of P.files) {
      const parts = p.split("/");
      let n = root;
      for (const d of parts.slice(0, -1)) n = n.dirs[d] || (n.dirs[d] = { dirs: {}, files: [] });
      n.files.push(p);
    }
    const changedDirs = new Set();
    Object.keys(sm).forEach(p => { const parts = p.split("/"); for (let i = 1; i < parts.length; i++) changedDirs.add(parts.slice(0, i).join("/")); });
    const out = [];
    const walk = (n, prefix, depth) => {
      Object.keys(n.dirs).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" })).forEach(d => {
        const full = prefix ? prefix + "/" + d : d;
        const open = P.openDirs.has(full);
        out.push('<div class="td' + (changedDirs.has(full) ? " changed" : "") + '" data-dir="' + esc(full) + '" style="padding-left:' + (8 + depth * 14) +
          'px"><i>' + (open ? "▾" : "▸") + "</i>" + esc(d) + "</div>");
        if (open) walk(n.dirs[d], full, depth + 1);
      });
      n.files.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" })).forEach(p => out.push(fileRow(p, p.split("/").pop(), depth)));
    };
    walk(root, "", 0);
    $("#tree").innerHTML = out.join("") || '<p class="faint small">No files yet.</p>';
  }

  // editor
  async function openFile(path, quiet) {
    if (P.file && P.file.dirty && P.file.path !== path && !confirm("Discard unsaved changes to " + P.file.path + "?")) return;
    const r = await api("GET", "projects/" + P.cur + "/file?path=" + encodeURIComponent(path));
    P.file = { project: P.cur, path: r.path, text: r.text, reason: r.reason, mtime: r.mtime, dirty: false };
    P.diffPath = null;
    const parts = r.path.split("/");
    for (let i = 1; i < parts.length; i++) P.openDirs.add(parts.slice(0, i).join("/"));
    if (!quiet) renderTree();
    renderEditor();
  }
  function renderEditor() {
    const f = P.file;
    const diffing = !!P.diffPath;
    $("#edEmpty").hidden = !!f || diffing;
    $("#edCode").hidden = !f || diffing || f.text == null;
    $("#diffView").hidden = !diffing;
    $("#edPath").textContent = diffing ? P.diffPath + " (changes)" : f ? f.path : "No file open";
    $("#edSave").hidden = $("#edDelete").hidden = $("#edAsk").hidden = !f || diffing || f.text == null;
    $("#edDiff").hidden = !f || diffing || !statusMap()[f.path];
    $("#edState").textContent = f && !diffing ? (f.text == null ? f.reason || "" : f.dirty ? "● unsaved" : "") : "";
    if (diffing && f) $("#edDiff").hidden = true;
    if (!f || diffing) return;
    if (f.text == null) { $("#edEmpty").hidden = false; $("#edEmpty").textContent = f.reason || "Can't open this file."; return; }
    $("#edEmpty").textContent = "Pick a file on the left. Ctrl+S saves.";
    const code = $("#code");
    if (code.value !== f.text) code.value = f.text;
    gutter();
  }
  function gutter() {
    const n = $("#code").value.split("\n").length;
    let s = "";
    for (let i = 1; i <= n; i++) s += i + "\n";
    $("#gutter").textContent = s;
    $("#gutter").scrollTop = $("#code").scrollTop;
  }
  async function save(force) {
    const f = P.file;
    if (!f || f.text == null) return;
    try {
      const r = await api("PUT", "projects/" + f.project + "/file", { path: f.path, text: $("#code").value, mtime: f.mtime, force: !!force });
      f.text = $("#code").value;
      f.mtime = r.mtime;
      f.dirty = false;
      renderEditor();
      toast("Saved " + f.path);
      refresh();
    } catch (e) {
      if (/changed on disk/.test(e.message)) {
        if (confirm(e.message + "\n\nOverwrite it with your version?")) await save(true);
        return;
      }
      throw e;
    }
  }
  function indentUnit(text) {
    const lines = text.split("\n");
    const tabs = lines.filter(l => l.startsWith("\t")).length;
    const two = lines.filter(l => /^ {2}\S/.test(l)).length, four = lines.filter(l => /^ {4}\S/.test(l)).length;
    return tabs > two + four ? "\t" : two > four ? "  " : "    ";
  }
  async function newFile() {
    const name = prompt("New file (path inside the project, e.g. src/utils.py):");
    if (!name || !name.trim()) return;
    const r = await api("PUT", "projects/" + P.cur + "/file", { path: name.trim(), text: "", create: true });
    await refresh();
    await openFile(r.path);
    $("#code").focus();
  }
  async function deleteFile() {
    const f = P.file;
    if (!f || !confirm("Delete " + f.path + "? " + (statusMap()[f.path] && statusMap()[f.path].code === "?" ? "It isn't in Git, so it can't be recovered." : "You can get it back from Git until you commit."))) return;
    await api("DELETE", "projects/" + f.project + "/file?path=" + encodeURIComponent(f.path));
    P.file = null;
    await refresh();
    renderEditor();
    toast("Deleted " + f.path);
  }

  // changes + commit
  function renderChanges() {
    const g = P.status || {};
    const files = g.files || [];
    $("#chgCount").textContent = files.length ? files.length : "";
    $("#commitBox").hidden = !g.is_repo;
    if (!g.is_repo) { $("#changes").innerHTML = '<p class="faint small">Not a Git repository.</p>'; return; }
    $("#changes").innerHTML = files.length ? files.map(f => '<div class="cf' + (P.diffPath === f.path ? " active" : "") + '" data-diff="' + esc(f.path) + '" title="' + esc(f.path) + '">' +
      '<input type="checkbox" data-pick="' + esc(f.path) + '"' + (P.unchecked.has(f.path) ? "" : " checked") + ' title="Include in the commit">' +
      '<span class="nm">' + esc(f.path.split("/").pop()) + ' <span class="faint">' + esc(f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "") + "</span></span>" +
      '<b class="st ' + (CODE_CLASS[f.code] || "mod") + '" title="' + esc(f.status) + '">' + (f.code === "?" ? "U" : esc(f.code)) + "</b>" +
      '<button class="icon-btn" data-discard="' + esc(f.path) + '" title="Discard changes">↺</button></div>').join("")
      : '<p class="faint small">No changes. Edit files here or ask the AI, and they show up here to commit.</p>';
  }
  function picked() {
    return ((P.status && P.status.files) || []).filter(f => !P.unchecked.has(f.path)).flatMap(f => f.orig ? [f.path, f.orig] : [f.path]);
  }
  async function showDiff(path) {
    P.diffPath = path;
    $$("#changes .cf").forEach(el => el.classList.toggle("active", el.dataset.diff === path));
    const r = await api("GET", "projects/" + P.cur + "/diff?path=" + encodeURIComponent(path));
    if (P.diffPath !== path) return;
    $("#diffView").innerHTML = r.reason ? '<p class="faint small">' + esc(r.reason) + "</p>" : renderDiff(r.diff);
    renderEditor();
  }
  async function commit() {
    const msg = $("#commitMsg").value.trim();
    if (!msg) { toast("Write a commit message (or press ✨ Write message).", true); $("#commitMsg").focus(); return; }
    const all = (P.status.files || []).length, n = (P.status.files || []).filter(f => !P.unchecked.has(f.path)).length;
    if (!n) { toast("Tick at least one file to commit.", true); return; }
    await gitOp("commit", { message: msg, paths: n === all ? [] : picked() });
    $("#commitMsg").value = "";
    P.unchecked = new Set();
    P.diffPath = null;
    renderEditor();
    refresh();
  }
  async function genMessage() {
    const b = $("#genMsg");
    b.disabled = true;
    b.textContent = "Writing…";
    try {
      const all = (P.status.files || []).length, n = picked().length;
      const r = await api("POST", "projects/" + P.cur + "/commit_message", { paths: n === all ? [] : picked(), model: app.currentModel() });
      $("#commitMsg").value = r.message;
    } finally {
      b.disabled = false;
      b.textContent = "✨ Write message";
    }
  }
  async function discard(path) {
    const f = ((P.status && P.status.files) || []).find(x => x.path === path);
    if (!f || !confirm(f.code === "?" ? "Delete the new file " + path + "? It isn't in Git, so it can't be recovered." :
      "Throw away your changes to " + path + " and go back to the last commit?")) return;
    await api("POST", "projects/" + P.cur + "/git", { op: "discard", path });
    if (P.diffPath === path) P.diffPath = null;
    if (P.file && P.file.path === path) P.file.dirty = false;
    await refresh(true);
    toast("Discarded changes to " + path);
  }

  function renderHistory() {
    const g = P.status || {};
    $("#history").innerHTML = g.is_repo ? ((g.log || []).map(c => '<div class="hc"><div>' + esc(c.subject) + '</div><div class="faint small"><code>' + esc(c.hash) +
      "</code> · " + esc(c.author) + " · " + esc(c.when) + "</div></div>").join("") || '<p class="faint small">No commits yet.</p>') : '<p class="faint small">Not a Git repository.</p>';
  }

  function setSide(name) {
    P.side = name;
    $$(".side-tabs .tab").forEach(t => t.classList.toggle("active", t.dataset.side === name));
    $("#sideFiles").hidden = name !== "files";
    $("#sideChanges").hidden = name !== "changes";
    $("#sideHistory").hidden = name !== "history";
    if (name !== "changes" && P.diffPath) { P.diffPath = null; renderEditor(); }
  }

  // terminal
  function renderTerm() {
    const out = $("#termOut");
    out.textContent = P.term[P.cur] || "";
    out.scrollTop = out.scrollHeight;
    const running = !!P.running[P.cur];
    $("#termStop").hidden = !running;
    $("#termRun").hidden = running;
  }
  function termWrite(pid, text) {
    let t = (P.term[pid] || "") + text;
    if (t.length > 200000) t = t.slice(-150000);
    P.term[pid] = t;
    if (pid === P.cur) {
      const out = $("#termOut");
      const stick = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
      out.textContent = t;
      if (stick) out.scrollTop = out.scrollHeight;
    }
  }
  async function termRun() {
    const cmd = $("#termCmd").value.trim();
    if (!cmd || !P.cur) return;
    const pid = P.cur;
    termWrite(pid, (P.term[pid] ? "\n" : "") + "$ " + cmd + "\n");
    $("#term").classList.remove("collapsed");
    const r = await api("POST", "projects/" + pid + "/terminal", { command: cmd });
    P.running[pid] = r.run;
    $("#termCmd").value = "";
    P.hist = [cmd].concat((P.hist || []).filter(x => x !== cmd)).slice(0, 50);
    P.histI = -1;
    renderTerm();
  }

  // add a project
  function addDialog(kind) {
    const tabs = '<div class="tabs add-tabs">' + [["open", "Open a folder"], ["clone", "Clone from GitHub"], ["zip", "Import a ZIP"]].map(([k, l]) =>
      '<button class="tab' + (k === kind ? " active" : "") + '" data-k="' + k + '">' + l + "</button>").join("") + "</div>";
    let body = "";
    if (kind === "open") {
      body = '<p class="faint small">Pick the project\'s folder (the one with its .git, package.json, etc.). Files are worked on in place; nothing is copied.</p>' +
        '<label>Folder<span class="row tight"><input id="fsPath" spellcheck="false"><button class="btn" data-a="go">Go</button></span></label>' +
        '<div class="fs" id="fsList"></div><p class="small" id="fsMsg"></p>' +
        '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="open">Open this folder</button></div>';
    } else if (kind === "clone") {
      body = '<p class="faint small">Uses the Git on this computer and your existing login for GitHub (Git Credential Manager, SSH key, or <code>gh auth login</code>). Private repos work if they work in your terminal.</p>' +
        '<label>Repository address<input id="cloneUrl" placeholder="https://github.com/you/project.git" spellcheck="false"></label>' +
        '<label>Save in<input id="cloneParent" spellcheck="false" value="' + esc(P.defaultParent) + '"></label><p class="small" id="fsMsg"></p>' +
        '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="clone">Clone</button></div>';
    } else {
      body = '<p class="faint small">Unpacks the ZIP into a new folder and opens it. (It won\'t be a Git repository unless the ZIP has its .git folder; you can initialize one.)</p>' +
        '<label>ZIP file<input type="file" id="zipFile" accept=".zip,application/zip"></label>' +
        '<label>Unpack in<input id="zipParent" spellcheck="false" value="' + esc(P.defaultParent) + '"></label><p class="small" id="fsMsg"></p>' +
        '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="zip">Import</button></div>';
    }
    const d = openModal("<h3>Add a project</h3>" + tabs + body);
    d.classList.add("wide");
    d.querySelectorAll("[data-k]").forEach(t => (t.onclick = () => addDialog(t.dataset.k)));
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    const msg = (t, bad) => { $("#fsMsg").textContent = t; $("#fsMsg").style.color = bad ? "var(--red)" : "var(--dim)"; };
    const added = async r => {
      closeModal();
      P.cur = r.project.id;
      P.file = null;
      await show();
      toast("Added " + r.project.name);
    };
    if (kind === "open") {
      const browse = guard(async path => {
        let r;
        try { r = await api("GET", "fs?path=" + encodeURIComponent(path || "")); } catch (e) { msg(e.message, true); return; }
        $("#fsPath").value = r.path;
        msg(r.is_repo ? "This folder is a Git repository." : "", false);
        $("#fsList").innerHTML = (r.parent ? '<div class="fd up" data-p="' + esc(r.parent) + '">↑ ..</div>' : "") +
          (r.drives || []).map(dv => '<div class="fd drive" data-p="' + esc(dv) + '">' + esc(dv) + "</div>").join("") +
          r.dirs.map(n => '<div class="fd" data-p="' + esc(r.path.replace(/[\\/]$/, "") + (r.path.includes("\\") ? "\\" : "/") + n) + '">📁 ' + esc(n) + "</div>").join("");
        $("#fsList").querySelectorAll("[data-p]").forEach(el => (el.onclick = () => browse(el.dataset.p)));
      });
      browse(store("va-last-folder") || "");
      d.querySelector('[data-a="go"]').onclick = () => browse($("#fsPath").value);
      $("#fsPath").onkeydown = e => { if (e.key === "Enter") browse($("#fsPath").value); };
      d.querySelector('[data-a="open"]').onclick = guard(async () => {
        try {
          const r = await api("POST", "projects", { action: "open", path: $("#fsPath").value });
          store("va-last-folder", $("#fsPath").value.replace(/[\\/][^\\/]+[\\/]?$/, ""));
          await added(r);
        } catch (e) { msg(e.message, true); }
      });
    } else if (kind === "clone") {
      setTimeout(() => $("#cloneUrl") && $("#cloneUrl").focus(), 30);
      d.querySelector('[data-a="clone"]').onclick = guard(async () => {
        msg("Cloning… (big repositories can take a while)");
        const b = d.querySelector('[data-a="clone"]');
        b.disabled = true;
        try { await added(await api("POST", "projects", { action: "clone", url: $("#cloneUrl").value, parent: $("#cloneParent").value })); }
        catch (e) { msg(e.message, true); b.disabled = false; }
      });
    } else {
      d.querySelector('[data-a="zip"]').onclick = guard(async () => {
        const f = $("#zipFile").files[0];
        if (!f) { msg("Choose a ZIP file.", true); return; }
        msg("Unpacking…");
        const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
        try { await added(await api("POST", "projects", { action: "zip", filename: f.name, data, parent: $("#zipParent").value })); }
        catch (e) { msg(e.message, true); }
      });
    }
  }

  function settingsDialog() {
    const p = cur();
    if (!p) return;
    const d = openModal("<h3>Project settings</h3>" +
      '<label>Name<input id="psName" value="' + esc(p.name) + '"></label>' +
      '<p class="faint small">Folder: <code>' + esc(p.path) + "</code></p>" +
      '<label>Notes for the AI (how to run it, conventions, things to avoid). Added to every chat about this project.<textarea id="psNotes" rows="6" placeholder="e.g. Run tests with: pytest -q&#10;Use type hints. Don\'t touch the migrations folder.">' + esc(p.notes || "") + "</textarea></label>" +
      '<label>Commands the AI may run without asking, one per line (exact start of the command, e.g. <code>pytest</code>, <code>npm test</code>). Commands that chain others (; && |) always ask.' +
      '<textarea id="psAllowed" rows="3" spellcheck="false">' + esc((p.allowed_commands || []).join("\n")) + "</textarea></label>" +
      '<label>Protected branches: the AI can\'t push to these (you still can), comma-separated, e.g. <code>main, master</code><input id="psProtected" spellcheck="false" value="' +
      esc((p.protected || []).join(", ")) + '"></label>' +
      '<div class="row between"><button class="btn danger" data-a="remove">Remove from the app</button><span><button class="btn" data-a="cancel">Cancel</button> <button class="btn primary" data-a="save">Save</button></span></div>' +
      '<p class="faint small">Removing only takes it off this list; the folder and its files stay where they are.</p>');
    d.classList.add("wide");
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="save"]').onclick = guard(async () => {
      await api("PATCH", "projects/" + p.id, { name: $("#psName").value, notes: $("#psNotes").value,
        allowed_commands: $("#psAllowed").value.split("\n").map(s => s.trim()).filter(Boolean),
        protected: $("#psProtected").value.split(",").map(s => s.trim()).filter(Boolean) });
      closeModal();
      await show();
      toast("Project settings saved");
    });
    d.querySelector('[data-a="remove"]').onclick = guard(async () => {
      if (!confirm("Remove " + p.name + " from the app? The folder and files are not deleted.")) return;
      await api("DELETE", "projects/" + p.id);
      closeModal();
      P.cur = null;
      P.file = null;
      await show();
      toast("Removed " + p.name);
    });
  }

  async function chatAbout(prefill) {
    const p = cur();
    if (!p) return;
    const c = app.S.chat;
    if (!(c && c.project === p.id && !app.S.busy.has(c.id))) {
      await app.newChat();
      await app.patchChat({ project: p.id, title: p.name });
      app.renderChat();
      app.renderRail();
    } else app.showView("chat");
    if (prefill) { $("#prompt").value = prefill; app.autosize(); }
    $("#prompt").focus();
  }

  // ── events ───────────────────────────────────────────────────────────
  function onEvent(ev) {
    if (ev.type === "term") termWrite(ev.project, ev.data);
    else if (ev.type === "term_done") {
      termWrite(ev.project, "\n[exit code " + ev.code + "]\n");
      delete P.running[ev.project];
      if (ev.project === P.cur) { renderTerm(); if (app.S.view === "projects") refresh(true); }
    } else if (ev.type === "chat" && app.S.view === "projects" && ev.chat.project && ev.chat.project === P.cur) {
      // the AI changed something in this project: keep the tree, changes and open file current
      const last = (ev.messages || [])[ev.messages.length - 1];
      if (last && last.role === "tool" && last.status !== "running" && !/^(read_file|list_files|search_code|git_status|git_diff|git_log)$/.test(last.name)) {
        clearTimeout(P.refreshTimer);
        P.refreshTimer = setTimeout(() => guard(refresh)(true), 300);
      }
    }
  }

  // ── wiring ───────────────────────────────────────────────────────────
  function wire() {
    $("#projAdd").onclick = () => addDialog("open");
    $$("#projEmpty [data-add]").forEach(b => (b.onclick = () => addDialog(b.dataset.add)));
    $("#projPick").onchange = guard(() => openProject($("#projPick").value));
    $("#projSettings").onclick = settingsDialog;
    $("#projChat").onclick = guard(() => chatAbout());
    $("#gitBar").onclick = guard(async e => {
      const b = e.target.closest("[data-g]");
      if (!b) return;
      const g = b.dataset.g;
      if (g === "branch") branchMenu();
      else if (g === "pull") await pull();
      else if (g === "push") await push();
      else if (g === "abort") { if (confirm("Abort the " + P.status.state + " and go back to how things were before it?")) await gitOp("abort"); }
      else await gitOp(g);
    });
    $$(".side-tabs .tab").forEach(t => (t.onclick = () => setSide(t.dataset.side)));
    $("#fileFilter").oninput = renderTree;
    $("#refreshFiles").onclick = guard(() => refresh(true));
    $("#newFile").onclick = guard(newFile);
    $("#tree").onclick = guard(async e => {
      const d = e.target.closest("[data-dir]");
      if (d) { const k = d.dataset.dir; P.openDirs.has(k) ? P.openDirs.delete(k) : P.openDirs.add(k); renderTree(); return; }
      const f = e.target.closest("[data-file]");
      if (f) { if (P.side !== "files") setSide("files"); await openFile(f.dataset.file); }
    });
    const code = $("#code");
    code.oninput = () => {
      if (!P.file) return;
      const dirty = code.value !== P.file.text;
      if (dirty !== P.file.dirty) { P.file.dirty = dirty; $("#edState").textContent = dirty ? "● unsaved" : ""; }
      gutter();
    };
    code.onscroll = () => ($("#gutter").scrollTop = code.scrollTop);
    code.onkeydown = e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); guard(save)(); return; }
      if (e.key === "Tab" && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        const unit = indentUnit(code.value);
        const s = code.selectionStart, en = code.selectionEnd, v = code.value;
        if (s === en && !e.shiftKey) { document.execCommand("insertText", false, unit); return; }
        const ls = v.lastIndexOf("\n", s - 1) + 1;
        const block = v.slice(ls, en);
        const changed = e.shiftKey ? block.replace(new RegExp("^(\\t| {1," + unit.length + "})", "gm"), "")
          : block.replace(/^/gm, unit);
        code.setSelectionRange(ls, en);
        document.execCommand("insertText", false, changed);
        code.setSelectionRange(ls, ls + changed.length);
      } else if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) {
        // keep the indentation of the line above
        const s = code.selectionStart, v = code.value;
        const line = v.slice(v.lastIndexOf("\n", s - 1) + 1, s);
        let ind = (/^[ \t]*/.exec(line) || [""])[0];
        if (/[:{[(]\s*$/.test(line)) ind += indentUnit(v);
        e.preventDefault();
        document.execCommand("insertText", false, "\n" + ind);
      }
    };
    $("#edSave").onclick = guard(() => save());
    $("#edDelete").onclick = guard(deleteFile);
    $("#edDiff").onclick = guard(async () => { setSide("changes"); await showDiff(P.file.path); });
    $("#edAsk").onclick = guard(() => chatAbout("In `" + P.file.path + "`: "));
    $("#changes").onclick = guard(async e => {
      const x = e.target.closest("[data-discard]");
      if (x) { e.stopPropagation(); await discard(x.dataset.discard); return; }
      const pk = e.target.closest("[data-pick]");
      if (pk) { pk.checked ? P.unchecked.delete(pk.dataset.pick) : P.unchecked.add(pk.dataset.pick); return; }
      const row = e.target.closest("[data-diff]");
      if (row) await showDiff(row.dataset.diff);
    });
    $("#commitBtn").onclick = guard(commit);
    $("#commitMsg").onkeydown = e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") guard(commit)(); };
    $("#genMsg").onclick = guard(genMessage);
    $("#history").onclick = () => {};
    $("#termRun").onclick = guard(termRun);
    $("#termStop").onclick = guard(async () => { await api("POST", "projects/" + P.cur + "/terminal", { stop: true }); });
    $("#termClear").onclick = () => { P.term[P.cur] = ""; renderTerm(); };
    $("#termToggle").onclick = () => { $("#term").classList.toggle("collapsed"); $("#termToggle").textContent = $("#term").classList.contains("collapsed") ? "▴" : "▾"; };
    $("#termCmd").onkeydown = e => {
      if (e.key === "Enter") { e.preventDefault(); guard(termRun)(); }
      else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        const h = P.hist || [];
        if (!h.length) return;
        P.histI = Math.max(-1, Math.min(h.length - 1, (P.histI ?? -1) + (e.key === "ArrowUp" ? 1 : -1)));
        $("#termCmd").value = P.histI >= 0 ? h[P.histI] : "";
        e.preventDefault();
      }
    };
    window.addEventListener("focus", () => { if (app.S.view === "projects" && P.cur) guard(refresh)(true); });
    window.addEventListener("beforeunload", e => { if (P.file && P.file.dirty) { e.preventDefault(); e.returnValue = ""; } });
  }

  function leaving() {
    if (P.file && P.file.dirty && !confirm("Discard unsaved changes to " + P.file.path + "?")) return false;
    if (P.file && P.file.dirty) { P.file.dirty = false; P.file.text = null; P.file = null; }
    return true;
  }

  wire();
  return { show, load, onEvent, renderDiff, confirmView, toolExtra, undo, pickForChat, projectName, leaving,
    get list() { return P.list; } };
};
