// The Context page: GitHub pull requests, Jira issues and email from the last sync, drawn as one band per
// thread (items that mention each other) with Jira / PR / Email columns and lines between linked items.
// app.js calls VAContext(app) once.
window.VAContext = function (app) {
  "use strict";
  const { api, toast, guard, openModal, closeModal, $, $$ } = app;
  const esc = v => app.esc(v == null ? "" : String(v));
  const C = { graph: null, sources: [], syncing: false, filter: "", loose: false, sel: "", loaded: false };
  const KINDS = [["jira", "Jira"], ["pr", "Pull requests"], ["mail", "Email"]];
  const COL = { jira: 0, pr: 1, mail: 2 };
  let adj = {};

  async function load() {
    const r = await api("GET", "context");
    C.graph = r.graph;
    C.sources = r.sources;
    C.syncing = r.syncing;
    C.loaded = true;
    adj = {};
    C.byId = {};
    C.graph.nodes.forEach(n => { C.byId[n.id] = n; adj[n.id] = new Set(); });
    C.graph.edges.forEach(([a, b]) => { if (adj[a] && adj[b]) { adj[a].add(b); adj[b].add(a); } });
    if (C.sel && !C.byId[C.sel]) C.sel = "";
  }

  async function show() {
    await load();
    render();
  }

  // ── drawing ───────────────────────────────────────────────────────────
  const when = s => {
    if (!s) return "";
    const d = new Date(s);
    if (isNaN(d)) return s.slice(0, 10);
    const days = (Date.now() - d) / 864e5;
    return days < 1 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : days < 300 ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : d.toLocaleDateString();
  };
  function hay(n) {
    return [n.id, n.title, n.who, n.state, n.text].concat(Object.values(n.fields || {})).join(" ").toLowerCase();
  }
  function matches(n) {
    const words = C.filter.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return true;
    const h = hay(n);
    return words.every(w => h.includes(w));
  }
  function card(n, hit) {
    const label = n.kind === "pr" ? n.id.replace(/^[^/]+\//, "") : n.kind === "mail" ? "" : n.id;
    return '<div class="ccard k-' + esc(n.kind) + (C.sel === n.id ? " sel" : "") + (hit ? "" : " miss") + '" data-id="' + esc(n.id) + '" tabindex="0">' +
      '<div class="cc-top">' + (label ? '<span class="cc-id">' + esc(label) + "</span>" : '<span class="cc-id">' + esc(n.who || "") + "</span>") +
      (n.state ? '<span class="cc-state st-' + esc(n.state.replace(/\W+/g, "-")) + '">' + esc(n.state) + "</span>" : "") +
      '<span class="cc-when">' + esc(when(n.updated)) + "</span></div>" +
      '<div class="cc-title">' + esc(n.title) + "</div>" +
      (n.kind !== "mail" && n.who ? '<div class="cc-who">' + esc(n.kind === "jira" ? "Assignee: " + n.who : n.who) + "</div>" : "") + "</div>";
  }
  function band(ids, title) {
    const cols = [[], [], []];
    ids.forEach(i => { const n = C.byId[i]; if (n) cols[COL[n.kind]].push(n); });
    return '<section class="cband"><div class="cb-title">' + esc(title) + '</div><div class="cb-grid">' +
      cols.map((list, k) => '<div class="cb-col" data-col="' + k + '">' + list.map(n => card(n, matches(n))).join("") + "</div>").join("") +
      '</div><svg class="clinks" aria-hidden="true"></svg></section>';
  }
  function threadTitle(ids) {
    const j = ids.map(i => C.byId[i]).find(n => n && n.kind === "jira");
    const p = ids.map(i => C.byId[i]).find(n => n && n.kind === "pr");
    const lead = j || p || C.byId[ids[0]];
    return lead ? (lead.kind === "mail" ? lead.title : lead.id + " · " + lead.title) : "";
  }
  function render() {
    const g = C.graph;
    const counts = { jira: 0, pr: 0, mail: 0 };
    g.nodes.forEach(n => counts[n.kind]++);
    const st = $("#ctxStatus");
    if (C.syncing) st.innerHTML = '<span class="spinner"></span> Syncing ' + esc(C.sources.join(", ")) + "…";
    else if (!g.synced) st.textContent = C.sources.length ? "Not synced yet." : "";
    else st.innerHTML = "Synced " + esc(when(g.synced)) + " · " + counts.pr + " pull requests, " + counts.jira + " Jira issues, " + counts.mail + " emails · " +
      g.threads.length + " thread" + (g.threads.length === 1 ? "" : "s") +
      (g.errors.length ? ' · <button class="linkish bad" id="ctxErrs">' + g.errors.length + " problem" + (g.errors.length === 1 ? "" : "s") + "</button>" : "");
    $("#ctxSync").disabled = C.syncing;
    $("#ctxLoose").checked = C.loose;
    const board = $("#ctxBoard");
    if (!C.sources.length && !g.nodes.length) {
      board.innerHTML = '<div class="ctx-empty"><h3>Connect your work</h3><p class="faint">Add GitHub repositories, your Jira site and an email account in <b>Settings › Work context</b>. ' +
        "A sync reads your recent pull requests, issues and email, and links the ones that mention each other: a Jira key like ABC-123 in a PR, a PR link in an email.</p>" +
        '<p class="faint small">Everything is read-only and stays on this computer. The model only reads the saved copy, in chats where you turn on <b>Context</b>.</p>' +
        '<button class="btn primary" data-a="settings">Open settings</button></div>';
      renderDetail();
      return;
    }
    if (!g.nodes.length) {
      board.innerHTML = '<div class="ctx-empty"><p class="faint">' + (C.syncing ? "Syncing…" : "Nothing synced yet. Press <b>Sync</b>.") + "</p></div>";
      renderDetail();
      return;
    }
    const threads = g.threads.filter(t => t.some(i => matches(C.byId[i])));
    const loose = C.loose ? g.loose.filter(i => matches(C.byId[i])) : [];
    board.innerHTML = '<div class="cb-head"><span>Jira</span><span>Pull requests</span><span>Email</span></div>' +
      threads.map(t => band(t, threadTitle(t))).join("") +
      (loose.length ? band(loose, "Not linked to anything") : "") +
      (!threads.length && !loose.length ? '<p class="faint small pad">' + (C.filter ? "Nothing matches." : g.loose.length ? "No linked items. Tick <b>Unlinked</b> to see the " + g.loose.length + " items that don't mention each other." : "") + "</p>" : "") +
      (!C.loose && g.loose.length && threads.length ? '<p class="faint tiny pad">' + g.loose.length + " item" + (g.loose.length === 1 ? " isn't" : "s aren't") + " linked to anything; tick <b>Unlinked</b> to show them.</p>" : "");
    requestAnimationFrame(drawLinks);
    renderDetail();
  }

  // lines between linked cards, drawn in each band's own SVG layer
  function drawLinks() {
    $$("#ctxBoard .cband").forEach(b => {
      const svg = b.querySelector(".clinks");
      const box = b.getBoundingClientRect();
      svg.setAttribute("width", box.width);
      svg.setAttribute("height", box.height);
      const pos = {};
      b.querySelectorAll(".ccard").forEach(el => {
        const r = el.getBoundingClientRect();
        pos[el.dataset.id] = { l: r.left - box.left, r: r.right - box.left, t: r.top - box.top, y: r.top - box.top + Math.min(22, r.height / 2), b: r.bottom - box.top };
      });
      let paths = "";
      const done = new Set();
      Object.keys(pos).forEach(a => (adj[a] || new Set()).forEach(bId => {
        if (!pos[bId] || done.has(bId + "|" + a)) return;
        done.add(a + "|" + bId);
        let x = C.byId[a], y = C.byId[bId];
        if (COL[x.kind] > COL[y.kind]) [x, y] = [y, x];
        const p = pos[x.id], q = pos[y.id];
        const gap = COL[y.kind] - COL[x.kind];
        let d, dashed = false;
        if (gap === 0) {           // same column: an arc on its left
          const lx = Math.min(p.l, q.l) - 2, bend = lx - 16;
          d = "M" + lx + " " + p.y + " C" + bend + " " + p.y + " " + bend + " " + q.y + " " + lx + " " + q.y;
        } else if (gap === 1) {
          const mx = (p.r + q.l) / 2;
          d = "M" + p.r + " " + p.y + " C" + mx + " " + p.y + " " + mx + " " + q.y + " " + q.l + " " + q.y;
        } else {                   // Jira ↔ email: around the PR column, along the top of the band
          dashed = true;
          const top = 28, r = 6, x1 = p.r + 10, x2 = q.l - 10;
          d = "M" + p.r + " " + p.y + " H" + (x1 - r) + " Q" + x1 + " " + p.y + " " + x1 + " " + (p.y - r) + " V" + (top + r) + " Q" + x1 + " " + top + " " + (x1 + r) + " " + top +
            " H" + (x2 - r) + " Q" + x2 + " " + top + " " + x2 + " " + (top + r) + " V" + (q.y - r) + " Q" + x2 + " " + q.y + " " + (x2 + r) + " " + q.y + " H" + q.l;
        }
        paths += '<path d="' + d + '" data-a="' + esc(x.id) + '" data-b="' + esc(y.id) + '"' + (dashed ? ' class="dash"' : "") + "/>";
      }));
      svg.innerHTML = paths;
    });
    if (C.sel) highlight(C.sel, true);
  }
  function highlight(id, on) {
    const board = $("#ctxBoard");
    board.classList.toggle("focus", !!on);
    $$("#ctxBoard .ccard.hl, #ctxBoard path.hl").forEach(el => el.classList.remove("hl"));
    if (!on) return;
    const near = new Set([id].concat(Array.from(adj[id] || [])));
    $$("#ctxBoard .ccard").forEach(el => { if (near.has(el.dataset.id)) el.classList.add("hl"); });
    $$("#ctxBoard path").forEach(p => { if (p.dataset.a === id || p.dataset.b === id) p.classList.add("hl"); });
  }

  // ── the detail panel ──────────────────────────────────────────────────
  function renderDetail() {
    const box = $("#ctxDetail");
    const n = C.sel && C.byId[C.sel];
    box.hidden = !n;
    $("#view-context").classList.toggle("has-detail", !!n);
    if (!n) return;
    const kind = { jira: "Jira issue", pr: "Pull request", mail: "Email" }[n.kind];
    const fields = Object.entries(n.fields || {}).filter(([, v]) => v);
    const linked = Array.from(adj[n.id] || []).map(i => C.byId[i]).filter(Boolean);
    box.innerHTML = '<div class="cd-head"><span class="tag k-' + esc(n.kind) + '">' + kind + '</span><button class="icon-btn" data-a="close" title="Close">' + icon("close") + "</button></div>" +
      "<h3>" + esc(n.title) + '</h3><p class="faint small mono">' + esc(n.kind === "mail" ? (n.fields || {}).from || n.who : n.id) + "</p>" +
      '<table class="cd-fields">' + (n.state ? "<tr><td>Status</td><td>" + esc(n.state) + "</td></tr>" : "") +
      (n.who ? "<tr><td>" + (n.kind === "jira" ? "Assignee" : n.kind === "pr" ? "Author" : "From") + "</td><td>" + esc(n.who) + "</td></tr>" : "") +
      (n.updated ? "<tr><td>Updated</td><td>" + esc(new Date(n.updated).toLocaleString()) + "</td></tr>" : "") +
      fields.map(([k, v]) => "<tr><td>" + esc(k.charAt(0).toUpperCase() + k.slice(1)) + "</td><td>" + esc(k === "created" && v ? new Date(v).toLocaleString() : v) + "</td></tr>").join("") + "</table>" +
      '<div class="row tight">' + (n.url ? '<a class="btn small" href="' + esc(n.url) + '" target="_blank" rel="noopener noreferrer">' + icon("external") + "Open</a>" : "") +
      '<button class="btn small primary" data-a="ask">' + icon("spark") + "Ask about this</button></div>" +
      '<div class="cd-text">' + esc(n.text || "(no description)") + "</div>" +
      (linked.length ? '<h4>Linked (' + linked.length + ")</h4>" + linked.map(m => '<button class="cd-link" data-id="' + esc(m.id) + '"><span class="tag k-' + esc(m.kind) + '">' +
        esc({ jira: "Jira", pr: "PR", mail: "Email" }[m.kind]) + "</span><span>" + esc(m.kind === "mail" ? m.title : m.id + " · " + m.title) + "</span></button>").join("")
        : '<p class="faint small">Not linked to anything else.</p>');
  }
  function select(id) {
    C.sel = C.sel === id ? "" : id;
    $$("#ctxBoard .ccard").forEach(el => el.classList.toggle("sel", el.dataset.id === C.sel));
    highlight(C.sel, !!C.sel);
    renderDetail();
    if (C.sel) {
      const el = $('#ctxBoard .ccard[data-id="' + CSS.escape(C.sel) + '"]');
      if (el) el.scrollIntoView({ block: "nearest" });
    }
  }
  async function askAbout(n) {
    await app.newChat();
    await app.patchChat({ context: true });
    app.renderChat();
    app.renderRail();
    app.showView("chat");
    const what = n.kind === "mail" ? 'the email "' + n.title + '" from ' + n.who : n.id + ' ("' + n.title + '")';
    $("#prompt").value = "What's the status of " + what + "? Summarize it together with everything linked to it, and tell me what still needs doing.";
    app.autosize();
    $("#prompt").focus();
  }

  // ── events ────────────────────────────────────────────────────────────
  async function sync() {
    await api("POST", "context", { action: "sync" });
    C.syncing = true;
    render();
  }
  async function onEvent(ev) {
    if (ev.type !== "context") return;
    if (ev.ok && !ev.errors.length) toast("Work context synced: " + ev.count + " items.");
    else toast((ev.ok ? "Synced, with problems: " : "Sync failed: ") + ev.errors[0], true);
    await load();
    if (app.S.view === "context") render();
  }
  function wire() {
    const board = $("#ctxBoard");
    board.addEventListener("click", e => {
      const c = e.target.closest(".ccard");
      if (c) return select(c.dataset.id);
      if (e.target.closest('[data-a="settings"]')) { app.showView("settings"); const p = $("#ctxSettings"); if (p) p.scrollIntoView(); }
    });
    board.addEventListener("keydown", e => { const c = e.target.closest(".ccard"); if (c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(c.dataset.id); } });
    board.addEventListener("mouseover", e => { const c = e.target.closest(".ccard"); if (c) highlight(c.dataset.id, true); });
    board.addEventListener("mouseout", e => { const c = e.target.closest(".ccard"); if (c && !c.contains(e.relatedTarget)) highlight(C.sel, !!C.sel); });
    $("#ctxDetail").addEventListener("click", guard(async e => {
      const b = e.target.closest("[data-a], .cd-link");
      if (!b) return;
      if (b.classList.contains("cd-link")) return select(b.dataset.id);
      if (b.dataset.a === "close") return select(C.sel);
      if (b.dataset.a === "ask") return askAbout(C.byId[C.sel]);
    }));
    $("#ctxSync").onclick = guard(sync);
    $("#ctxLoose").onchange = e => { C.loose = e.target.checked; render(); };
    let t = null;
    $("#ctxFilter").oninput = e => { clearTimeout(t); t = setTimeout(() => { C.filter = e.target.value.trim(); render(); }, 120); };
    $("#ctxStatus").addEventListener("click", e => {
      if (!e.target.closest("#ctxErrs")) return;
      const d = openModal("<h3>Problems in the last sync</h3><ul>" + C.graph.errors.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>" +
        '<p class="faint small">Fix them in Settings › Work context, then sync again. Everything else that synced is shown.</p><div class="row end"><button class="btn primary" data-a="close">Close</button></div>');
      d.querySelector('[data-a="close"]').onclick = closeModal;
    });
    if (window.ResizeObserver) new ResizeObserver(() => { if (app.S.view === "context") drawLinks(); }).observe(board);
  }
  wire();
  return { show, onEvent, load, state: C };
};
