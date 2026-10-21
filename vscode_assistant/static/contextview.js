// The Context page: GitHub pull requests, Jira issues and email from the last sync, drawn as one band per
// thread (items that mention each other) with Jira / PR / Email columns and lines between linked items.
// app.js calls VAContext(app) once.
window.VAContext = function (app) {
  "use strict";
  const { api, toast, guard, openModal, closeModal, $, $$ } = app;
  const esc = v => app.esc(v == null ? "" : String(v));
  const C = { graph: null, sources: [], syncing: false, filter: "", loose: false, sel: "", loaded: false,
    mode: "tree", collapsed: new Set(), shut: new Set(), roots: {}, seen: new Set(), parentOf: {}, limit: 40 };
  const PAGE = 40;     // threads drawn at a time in Tree and Columns; "Show more" adds another page
  try { C.mode = localStorage.getItem("va-ctx-mode") || "tree"; } catch (_) { /* private window */ }
  const KINDS = [["jira", "Jira"], ["pr", "Pull requests"], ["mail", "Email"], ["note", "Notes"]];
  const LABEL = { jira: "Jira issue", pr: "Pull request", mail: "Email", note: "Note" };
  const SHORT = { jira: "Jira", pr: "PR", mail: "Email", note: "Note" };
  let COL = { jira: 0, pr: 1, mail: 2 };
  let adj = {};
  const isManual = (a, b) => C.manual.has(a < b ? a + "|" + b : b + "|" + a);

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
    C.manual = new Set((C.graph.manual || []).map(([a, b]) => (a < b ? a + "|" + b : b + "|" + a)));
    if (C.sel && !C.byId[C.sel]) C.sel = "";
  }

  async function show() {
    await load();
    render();
  }

  // ── drawing ───────────────────────────────────────────────────────────
  // formatting dates is slow in bulk, so the formatters are made once and each date is formatted once
  const FMT = { time: new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" }), day: new Intl.DateTimeFormat([], { month: "short", day: "numeric" }),
    full: new Intl.DateTimeFormat() };
  const whenCache = new Map();
  const when = s => {
    if (!s) return "";
    let out = whenCache.get(s);
    if (out !== undefined) return out;
    const d = new Date(s);
    if (isNaN(d)) out = s.slice(0, 10);
    else {
      const days = (Date.now() - d) / 864e5;
      out = days < 1 ? FMT.time.format(d) : days < 300 ? FMT.day.format(d) : FMT.full.format(d);
    }
    whenCache.set(s, out);
    return out;
  };

  function hay(n) {
    if (n._hay === undefined) n._hay = [n.id, n.title, n.who, n.state, n.text].concat(Object.values(n.fields || {})).join(" ").toLowerCase();
    return n._hay;
  }
  let words = [], wordsFor = null;
  function matches(n) {
    if (wordsFor !== C.filter) { wordsFor = C.filter; words = C.filter.toLowerCase().split(/\s+/).filter(Boolean); }
    if (!words.length) return true;
    const h = hay(n);
    return words.every(w => h.includes(w));
  }
  function card(n, hit) {
    const label = n.kind === "pr" ? n.id.replace(/^[^/]+\//, "") : n.kind === "jira" ? n.id : n.kind === "note" ? (n.who || "note") : "";
    return '<div class="ccard k-' + esc(n.kind) + (C.sel === n.id ? " sel" : "") + (hit ? "" : " miss") + '" data-id="' + esc(n.id) + '" tabindex="0">' +
      '<div class="cc-top">' + (label ? '<span class="cc-id">' + esc(label) + "</span>" : '<span class="cc-id">' + esc(n.who || "") + "</span>") +
      (n.state ? '<span class="cc-state st-' + esc(n.state.replace(/\W+/g, "-")) + '">' + esc(n.state) + "</span>" : "") +
      '<span class="cc-when">' + esc(when(n.updated)) + "</span></div>" +
      '<div class="cc-title">' + esc(n.title) + "</div>" +
      (n.kind !== "mail" && n.kind !== "note" && n.who ? '<div class="cc-who">' + esc(n.kind === "jira" ? "Assignee: " + n.who : n.who) + "</div>" : "") + "</div>";
  }
  function band(ids, title) {
    const cols = C.kinds.map(() => []);
    ids.forEach(i => { const n = C.byId[i]; if (n) cols[COL[n.kind]].push(n); });
    return '<section class="cband"><div class="cb-title">' + esc(title) + '</div><div class="cb-grid">' +
      cols.map((list, k) => '<div class="cb-col" data-col="' + k + '">' + list.map(n => card(n, matches(n))).join("") + "</div>").join("") +
      '</div><svg class="clinks" aria-hidden="true"></svg></section>';
  }
  function threadTitle(ids) {
    const j = ids.map(i => C.byId[i]).find(n => n && n.kind === "jira");
    const p = ids.map(i => C.byId[i]).find(n => n && n.kind === "pr");
    const lead = j || p || C.byId[ids[0]];
    return lead ? (lead.kind === "mail" || lead.kind === "note" ? lead.title : lead.id + " · " + lead.title) : "";
  }
  function render() {
    const g = C.graph;
    const counts = { jira: 0, pr: 0, mail: 0, note: 0 };
    g.nodes.forEach(n => counts[n.kind]++);
    // a Notes column only once there are notes
    C.kinds = KINDS.filter(([k]) => k !== "note" || counts.note);
    COL = {};
    C.kinds.forEach(([k], i) => (COL[k] = i));
    $("#ctxBoard").style.setProperty("--ctx-cols", C.kinds.length);
    $("#ctxBoard").classList.toggle("cols4", C.kinds.length > 3);
    const notes = counts.note ? ", " + counts.note + " note" + (counts.note === 1 ? "" : "s") : "";
    const st = $("#ctxStatus");
    if (C.syncing) st.innerHTML = '<span class="spinner"></span> Syncing ' + esc(C.sources.join(", ")) + "…";
    else if (!g.synced) st.textContent = (C.sources.length ? "Not synced yet." : "Nothing synced (no sources set up).") + (notes ? " " + notes.slice(2) + "." : "");
    else st.innerHTML = "Synced " + esc(when(g.synced)) + " · " + counts.pr + " pull requests, " + counts.jira + " Jira issues, " + counts.mail + " emails" + esc(notes) + " · " +
      g.threads.length + " thread" + (g.threads.length === 1 ? "" : "s") +
      (g.errors.length ? ' · <button class="linkish bad" id="ctxErrs">' + g.errors.length + " problem" + (g.errors.length === 1 ? "" : "s") + "</button>" : "");
    $("#ctxSync").disabled = C.syncing;
    $("#ctxLoose").checked = C.loose;
    const board = $("#ctxBoard");
    if (!C.sources.length && !g.nodes.length) {
      board.innerHTML = '<div class="ctx-empty"><h3>Connect your work</h3><p class="faint">Add GitHub repositories, your Jira site and an email account in <b>Settings › Work context</b>. ' +
        "A sync reads your recent pull requests, issues and email, and links the ones that mention each other: a Jira key like ABC-123 in a PR, a PR link in an email.</p>" +
        '<p class="faint small">Everything is read-only and stays on this computer. The model only reads the saved copy, in chats where you turn on <b>Context</b>.</p>' +
        '<div class="row" style="justify-content:center"><button class="btn primary" data-a="settings">Open settings</button><button class="btn" data-a="addnote">' + icon("plus") + "Add a note</button></div>" +
        '<p class="faint small">Or add context by hand: notes from a meeting, a Slack thread, a decision, a document link. Notes that mention ABC-123 or a PR link to them automatically, and you can link any two items yourself.</p></div>';
      renderDetail();
      return;
    }
    if (!g.nodes.length) {
      board.innerHTML = '<div class="ctx-empty"><p class="faint">' + (C.syncing ? "Syncing…" : "Nothing synced yet. Press <b>Sync</b>.") + "</p></div>";
      renderDetail();
      return;
    }
    const threads = g.threads.filter(t => t.some(i => matches(C.byId[i])));
    $$("#ctxMode .tab").forEach(t => t.classList.toggle("active", t.dataset.mode === C.mode));
    board.classList.toggle("tree-mode", C.mode === "tree");
    board.classList.toggle("graph-mode", C.mode === "graph");
    // your own notes always show; other unlinked items only with Unlinked ticked
    const loose = g.loose.filter(i => (C.loose || C.byId[i].kind === "note") && matches(C.byId[i]));
    const hidden = g.loose.filter(i => C.byId[i].kind !== "note").length;
    if (C.mode !== "graph") stopGraph();
    if (C.mode === "graph") {
      const shown = new Set([].concat(...g.threads));
      g.loose.forEach(i => { if (C.loose || C.byId[i].kind === "note") shown.add(i); });
      renderGraph(Array.from(shown));
      renderDetail();
      return;
    }
    const more = threads.length > C.limit ? '<div class="ctx-more"><button class="btn" data-a="more">Show ' + Math.min(PAGE, threads.length - C.limit) + " more threads</button>" +
      '<span class="faint small">' + C.limit + " of " + threads.length + " shown" + (C.filter ? "" : " · the filter box finds the rest") + "</span></div>" : "";
    if (C.mode === "tree") {
      const loose2 = g.loose.filter(i => (C.loose || C.byId[i].kind === "note") && matches(C.byId[i]));
      board.innerHTML = threads.slice(0, C.limit).map(treeHTML).join("") + more +
        (loose2.length ? '<section class="tthread"><div class="tt-head"><span class="tt-title">' + (C.loose ? "Not linked to anything" : "Your notes that aren't linked yet") +
          '</span></div><ul class="ttree flat">' + loose2.map(i => "<li>" + rowHTML(i, null, 0, []) + "</li>").join("") + "</ul></section>" : "") +
        (!threads.length && !loose2.length ? '<p class="faint small pad">' + (C.filter ? "Nothing matches." : hidden ? "No linked items. Tick <b>Unlinked</b> to see the " + hidden + " items that don't mention each other." : "") + "</p>" : "") +
        (!C.loose && hidden && (threads.length || loose2.length) ? '<p class="faint tiny pad">' + hidden + " item" + (hidden === 1 ? " isn't" : "s aren't") + " linked to anything; tick <b>Unlinked</b> to show them.</p>" : "");
      if (C.sel) highlight(C.sel, true);
      renderDetail();
      return;
    }
    board.innerHTML = '<div class="cb-head">' + C.kinds.map(([, l]) => "<span>" + l + "</span>").join("") + "</div>" +
      threads.slice(0, C.limit).map(t => band(t, threadTitle(t))).join("") + more +
      (loose.length ? band(loose, C.loose ? "Not linked to anything" : "Your notes that aren't linked yet") : "") +
      (!threads.length && !loose.length ? '<p class="faint small pad">' + (C.filter ? "Nothing matches." : hidden ? "No linked items. Tick <b>Unlinked</b> to see the " + hidden + " items that don't mention each other." : "") + "</p>" : "") +
      (!C.loose && hidden && (threads.length || loose.length) ? '<p class="faint tiny pad">' + hidden + " item" + (hidden === 1 ? " isn't" : "s aren't") + " linked to anything; tick <b>Unlinked</b> to show them.</p>" : "");
    requestAnimationFrame(drawLinks);
    renderDetail();
  }

  // ── the tree: each thread as an outline, from its main issue down ──────
  const ORDER = { jira: 0, pr: 1, mail: 2, note: 3 };
  const REL = {
    "jira>pr": "worked on in", "jira>mail": "discussed in", "jira>note": "noted", "jira>jira": "related to",
    "pr>jira": "for", "pr>mail": "discussed in", "pr>note": "noted", "pr>pr": "related to",
    "mail>jira": "mentions", "mail>pr": "mentions", "mail>mail": "related to", "mail>note": "noted",
    "note>jira": "about", "note>pr": "about", "note>mail": "about", "note>note": "related to",
  };
  const threadKey = ids => ids.slice().sort()[0];
  function buildTree(ids) {
    const set = new Set(ids), key = threadKey(ids);
    const deg = i => Array.from(adj[i] || []).filter(j => set.has(j)).length;
    // the root: the issue everything hangs off (or a PR, or the most-connected item); you can pick another
    let root = C.roots[key] && set.has(C.roots[key]) ? C.roots[key] : null;
    if (!root) root = ids.slice().sort((a, b) => (ORDER[C.byId[a].kind] - ORDER[C.byId[b].kind]) || (deg(b) - deg(a)) || a.localeCompare(b))[0];
    const parent = { [root]: null }, kids = {}, depth = { [root]: 0 }, queue = [root];
    while (queue.length) {
      const cur = queue.shift();
      kids[cur] = Array.from(adj[cur] || []).filter(j => set.has(j) && !(j in parent))
        .sort((a, b) => (ORDER[C.byId[a].kind] - ORDER[C.byId[b].kind]) || (C.byId[b].updated || "").localeCompare(C.byId[a].updated || ""));
      kids[cur].forEach(j => { parent[j] = cur; depth[j] = depth[cur] + 1; queue.push(j); });
    }
    if (!C.seen.has(key)) {          // big threads start folded below the second level
      C.seen.add(key);
      if (ids.length > 12) Object.keys(kids).forEach(i => { if (depth[i] >= 1 && kids[i].length) C.collapsed.add(i); });
    }
    Object.assign(C.parentOf, parent);
    return { root, parent, kids, set };
  }
  function rowHTML(i, parentId, depth, kidList, set) {
    const n = C.byId[i], p = parentId ? C.byId[parentId] : null;
    const open = !C.collapsed.has(i) || !!C.filter;
    const rel = p ? REL[p.kind + ">" + n.kind] || "linked to" : "";
    // links that the tree can't show as branches: shown as small jump buttons
    const extra = set ? Array.from(adj[i] || []).filter(j => set.has(j) && j !== parentId && C.parentOf[j] !== i) : [];
    const label = n.kind === "pr" ? n.id.replace(/^[^/]+\//, "") : n.kind === "jira" ? n.id : "";
    return '<div class="ccard trow k-' + esc(n.kind) + (C.sel === i ? " sel" : "") + (matches(n) ? "" : " miss") + '" data-id="' + esc(i) + '" tabindex="0">' +
      (kidList.length ? '<button class="twisty" data-tog="' + esc(i) + '" title="' + (open ? "Fold" : "Unfold") + '">' + icon(open ? "chevron-down" : "chevron-right") +
        (open ? "" : '<span class="kidcount">' + countBelow(i) + "</span>") + "</button>" : '<span class="twisty-gap"></span>') +
      (rel ? '<span class="rel">' + esc(rel) + (p && isManual(parentId, i) ? ' <b class="byyou" title="You linked these">· by you</b>' : "") + "</span>" : "") +
      '<span class="tag k-' + esc(n.kind) + '">' + esc(SHORT[n.kind]) + "</span>" +
      (label ? '<span class="cc-id">' + esc(label) + "</span>" : n.kind === "note" && n.who && n.who !== "note" ? '<span class="cc-id">' + esc(n.who) + "</span>" : "") +
      '<span class="t-title">' + esc(n.title) + "</span>" +
      (n.state ? '<span class="cc-state st-' + esc(n.state.replace(/\W+/g, "-")) + '">' + esc(n.state) + "</span>" : "") +
      (n.who && n.kind !== "note" ? '<span class="t-who">' + esc(n.who) + "</span>" : "") +
      extra.map(j => '<button class="xlink" data-jump="' + esc(j) + '" title="Also linked to ' + esc(C.byId[j].title) + '">' + icon("link") + esc(C.byId[j].kind === "jira" || C.byId[j].kind === "pr" ? C.byId[j].id.replace(/^[^/]+\//, "") : SHORT[C.byId[j].kind]) + "</button>").join("") +
      '<span class="cc-when">' + esc(when(n.updated)) + "</span></div>";
  }
  let treeCache = null;
  function countBelow(i) {
    let c = 0;
    const walk = x => (treeCache.kids[x] || []).forEach(k => { c++; walk(k); });
    walk(i);
    return c;
  }
  function treeHTML(ids) {
    const t = treeCache = buildTree(ids), key = threadKey(ids);
    const node = (i, depth) => {
      const kids = t.kids[i] || [], open = !C.collapsed.has(i) || !!C.filter;
      return "<li>" + rowHTML(i, t.parent[i], depth, kids, t.set) + (kids.length && open ? '<ul class="ttree">' + kids.map(k => node(k, depth + 1)).join("") + "</ul>" : "") + "</li>";
    };
    const counts = {};
    ids.forEach(i => (counts[C.byId[i].kind] = (counts[C.byId[i].kind] || 0) + 1));
    const words = { jira: ["issue", "issues"], pr: ["pull request", "pull requests"], mail: ["email", "emails"], note: ["note", "notes"] };
    const shut = C.shut.has(key) && !C.filter;
    return '<section class="tthread' + (shut ? " shut" : "") + '"><div class="tt-head"><button class="twisty" data-shut="' + esc(key) + '">' + icon(shut ? "chevron-right" : "chevron-down") + "</button>" +
      '<span class="tt-title">' + esc(threadTitle([t.root])) + '</span><span class="faint tiny">' +
      KINDS.filter(([k]) => counts[k]).map(([k]) => counts[k] + " " + words[k][counts[k] === 1 ? 0 : 1]).join(" · ") + "</span>" +
      '<span class="tt-acts">' + (C.roots[key] ? '<button class="linkish" data-unroot="' + esc(key) + '">Back to the main item</button>' : "") + '<button class="linkish" data-all="open" data-key="' + esc(key) + '">Unfold all</button><button class="linkish" data-all="fold" data-key="' + esc(key) + '">Fold all</button></span></div>' +
      (shut ? "" : '<ul class="ttree root">' + node(t.root, 0) + "</ul>") + "</section>";
  }
  function threadOf(id) { return C.graph.threads.find(t => t.includes(id)); }
  function reveal(id) {     // unfold everything above an item so it can be seen
    const t = threadOf(id);
    if (t) {
      C.shut.delete(threadKey(t));
      const at = C.graph.threads.filter(x => x.some(i => matches(C.byId[i]))).indexOf(t);
      if (at >= C.limit) C.limit = at + 1;     // a thread past "Show more"
    }
    for (let p = C.parentOf[id]; p; p = C.parentOf[p]) C.collapsed.delete(p);
  }

  // ── the graph: every item a dot, every link a line, laid out by a small force simulation ──
  const KCOL = { jira: "", pr: "", mail: "", note: "" };     // read from the theme (--k-jira…) when the graph draws
  const GV = { nodes: [], links: [], at: {}, pos: {}, cam: { x: 0, y: 0, k: 1 }, alpha: 0, raf: 0, hover: "", drag: null, pan: null, fitted: false, colors: null };
  function renderGraph(ids) {
    const board = $("#ctxBoard");
    if (!board.querySelector(".gv")) {
      board.innerHTML = '<div class="gv"><canvas id="ctxCanvas" tabindex="0" aria-label="Graph of linked items"></canvas>' +
        '<div class="gv-tools"><button class="icon-btn" data-gv="in" title="Zoom in">' + icon("plus") + '</button><button class="icon-btn" data-gv="out" title="Zoom out">' +
        '<svg class="i" viewBox="0 0 16 16"><path d="M3 8h10"/></svg></button><button class="icon-btn" data-gv="fit" title="Fit everything">' + icon("refresh") + "</button></div>" +
        '<div class="gv-legend">' + KINDS.map(([k, l]) => '<span><i style="background:var(--k-' + k + ')"></i>' + l + "</span>").join("") +
        '<span class="faint">Drag to move · scroll to zoom · click for details</span></div></div>';
      wireGraph(board.querySelector("canvas"));
      GV.fitted = false;
    }
    const css = getComputedStyle(document.documentElement);
    Object.keys(KCOL).forEach(k => (KCOL[k] = css.getPropertyValue("--k-" + k).trim() || "#888"));
    GV.colors = { line: css.getPropertyValue("--line-2").trim() || "#444", text: css.getPropertyValue("--text").trim() || "#ddd",
      faint: css.getPropertyValue("--faint").trim() || "#777", accent: css.getPropertyValue("--accent").trim() || "#d97757", bg: css.getPropertyValue("--panel-2").trim() || "#222" };
    const set = new Set(ids), fresh = [];
    GV.nodes = ids.map(i => {
      const n = C.byId[i];
      let p = GV.pos[i];
      if (!p) { p = GV.pos[i] = { x: 0, y: 0, vx: 0, vy: 0 }; fresh.push(i); }
      return Object.assign(p, { id: i, kind: n.kind, deg: Array.from(adj[i] || []).filter(j => set.has(j)).length, hit: matches(n) });
    });
    GV.at = {};
    GV.nodes.forEach(n => (GV.at[n.id] = n));
    // each thread gets its own spot, biggest in the middle and the rest spiralling out, so separate threads read as separate islands
    const groups = C.graph.threads.map(t => t.filter(i => set.has(i))).filter(t => t.length)
      .concat(ids.filter(i => !(adj[i] || new Set()).size).map(i => [i]));
    let ring = 0, angle = 0;
    groups.forEach((grp, gi) => {
      const size = Math.sqrt(grp.length) * 85;
      let ax = 0, ay = 0;
      if (gi > 0) { ring = Math.max(ring, 260) + size * 0.35; angle += size / ring + 0.5; ax = Math.cos(angle) * ring; ay = Math.sin(angle) * ring; ring += size * 0.08; }
      grp.forEach(i => { GV.at[i].ax = ax; GV.at[i].ay = ay; });
    });
    GV.links = C.graph.edges.filter(([a, b]) => set.has(a) && set.has(b)).map(([a, b]) => ({ a: GV.at[a], b: GV.at[b], hand: isManual(a, b) }));
    // new items start next to something they link to (or on a ring), so the layout settles quickly
    fresh.forEach((i, k) => {
      const nb = Array.from(adj[i] || []).map(j => GV.pos[j]).find(q => q && !fresh.includes(q.id));
      const p = GV.pos[i], a = k * 2.4;
      const home = GV.at[i] || {};
      if (nb) { p.x = nb.x + Math.cos(a) * 30; p.y = nb.y + Math.sin(a) * 30; }
      else { const r = 20 + 10 * Math.sqrt(k); p.x = (home.ax || 0) + Math.cos(a) * r; p.y = (home.ay || 0) + Math.sin(a) * r; }
    });
    graphResize();
    if (fresh.length) {
      GV.alpha = 1;
      const until = performance.now() + 450;          // settle before the first frame, for at most about half a second
      for (let t = 0; t < 300 && performance.now() < until; t++) graphTick();
    }
    if (!GV.fitted) { graphFit(); GV.fitted = true; GV.touched = false; }
    GV.alpha = Math.max(GV.alpha, fresh.length ? 0.3 : 0.05);
    graphRun();
  }
  // Barnes-Hut: distant groups of dots push as one, so a tick costs n·log n instead of n² (3000+ items stay smooth)
  function bhBuild(ns) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    ns.forEach(p => { if (p.x < x0) x0 = p.x; if (p.y < y0) y0 = p.y; if (p.x > x1) x1 = p.x; if (p.y > y1) y1 = p.y; });
    const root = { x: x0, y: y0, s: Math.max(x1 - x0, y1 - y0) + 1, m: 0, sx: 0, sy: 0, p: null, kids: null, more: null };
    const insert = (c, p, depth) => {
      c.m++; c.sx += p.x; c.sy += p.y;
      if (!c.kids) {
        if (!c.p) { c.p = p; return; }
        if (depth > 24) { (c.more = c.more || []).push(p); return; }   // dots on the same spot
        const q = c.p;
        c.p = null; c.kids = [null, null, null, null];
        place(c, q, depth);
      }
      place(c, p, depth);
    };
    const place = (c, p, depth) => {
      const h = c.s / 2, right = p.x >= c.x + h, down = p.y >= c.y + h, k = (right ? 1 : 0) + (down ? 2 : 0);
      if (!c.kids[k]) c.kids[k] = { x: c.x + (right ? h : 0), y: c.y + (down ? h : 0), s: h, m: 0, sx: 0, sy: 0, p: null, kids: null, more: null };
      insert(c.kids[k], p, depth + 1);
    };
    ns.forEach(p => insert(root, p, 0));
    return root;
  }
  const push = (p, dx, dy, d2, m, al) => {      // dx, dy: from p to the other dot(s)
    if (d2 > 640000) return;
    if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
    const d = Math.sqrt(d2), f = -(1400 / d2 + 3 / d) * al * m;
    p.vx += dx / d * f; p.vy += dy / d * f;
  };
  function graphTick() {
    const ns = GV.nodes, n = ns.length, al = GV.alpha;
    if (!n) return;
    const root = bhBuild(ns), stack = [];
    for (let i = 0; i < n; i++) {               // every dot is pushed away from the others
      const p = ns[i];
      stack.length = 0; stack.push(root);
      while (stack.length) {
        const c = stack.pop();
        if (!c.m) continue;
        if (!c.kids) {
          if (c.p && c.p !== p) push(p, c.p.x - p.x, c.p.y - p.y, (c.p.x - p.x) ** 2 + (c.p.y - p.y) ** 2, 1, al);
          if (c.more) c.more.forEach(q => { if (q !== p) push(p, q.x - p.x, q.y - p.y, (q.x - p.x) ** 2 + (q.y - p.y) ** 2, 1, al); });
          continue;
        }
        const cx = c.sx / c.m, cy = c.sy / c.m, dx = cx - p.x, dy = cy - p.y, d2 = dx * dx + dy * dy;
        if (c.s * c.s < 0.64 * d2) push(p, dx, dy, d2, c.m, al);     // far enough: the whole cell as one
        else for (const k of c.kids) if (k) stack.push(k);
      }
    }
    GV.links.forEach(l => {                     // links pull together, like springs
      const dx = l.b.x - l.a.x, dy = l.b.y - l.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const want = 80 + 8 * Math.min(l.a.deg, l.b.deg), f = (d - want) / d * 0.1 * al;
      l.a.vx += dx * f; l.a.vy += dy * f; l.b.vx -= dx * f; l.b.vy -= dy * f;
    });
    ns.forEach(p => {                           // a pull towards its thread's spot keeps each thread an island
      const g = (p.deg ? 0.007 : 0.03) * al;
      p.vx -= (p.x - (p.ax || 0)) * g; p.vy -= (p.y - (p.ay || 0)) * g;
      if (p.fixed) { p.vx = p.vy = 0; return; }
      p.vx *= 0.6; p.vy *= 0.6;
      p.x += p.vx; p.y += p.vy;
    });
    // and never closer than their size plus room for a name (checked through a grid, not every pair)
    const cell = 64, grid = new Map();
    ns.forEach(p => { const key = Math.floor(p.x / cell) + "," + Math.floor(p.y / cell); let b = grid.get(key); if (!b) grid.set(key, (b = [])); b.push(p); });
    ns.forEach(p => {
      const gx = Math.floor(p.x / cell), gy = Math.floor(p.y / cell), rp = radius(p);
      for (let ox = -1; ox <= 1; ox++) for (let oy = -1; oy <= 1; oy++) {
        const b = grid.get((gx + ox) + "," + (gy + oy));
        if (!b) continue;
        for (const q of b) {
          if (q === p || q.id < p.id) continue;
          const dx = q.x - p.x, dy = q.y - p.y, min = Math.min(cell - 2, rp + radius(q) + 26), d2 = dx * dx + dy * dy;
          if (d2 >= min * min || d2 === 0) continue;
          const d = Math.sqrt(d2), k = (min - d) / d * 0.5;
          if (!p.fixed) { p.x -= dx * k; p.y -= dy * k; }
          if (!q.fixed) { q.x += dx * k; q.y += dy * k; }
        }
      }
    });
    GV.alpha *= 0.985;
  }
  function graphRun() {
    if (GV.raf) return;
    const step = () => {
      GV.raf = 0;
      if (app.S.view !== "context" || C.mode !== "graph") return;
      if (GV.alpha > 0.004 || GV.drag) { graphTick(); GV.raf = requestAnimationFrame(step); }
      else if (!GV.touched) { graphFit(); GV.touched = true; }   // once it settles, frame it (until you pan or zoom yourself)
      graphDraw();
    };
    GV.raf = requestAnimationFrame(step);
  }
  function stopGraph() { if (GV.raf) cancelAnimationFrame(GV.raf); GV.raf = 0; }
  function graphResize() {
    const cv = $("#ctxCanvas");
    if (!cv) return;
    const r = cv.parentElement.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, r.width * dpr); cv.height = Math.max(1, r.height * dpr);
    cv.style.width = r.width + "px"; cv.style.height = r.height + "px";
    GV.w = r.width; GV.h = r.height;
    graphDraw();
  }
  const radius = n => 5 + Math.sqrt(n.deg) * 2.6;
  const toScreen = p => ({ x: (p.x - GV.cam.x) * GV.cam.k + GV.w / 2, y: (p.y - GV.cam.y) * GV.cam.k + GV.h / 2 });
  const toWorld = (sx, sy) => ({ x: (sx - GV.w / 2) / GV.cam.k + GV.cam.x, y: (sy - GV.h / 2) / GV.cam.k + GV.cam.y });
  function graphFit() {
    if (!GV.nodes.length || !GV.w) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    GV.nodes.forEach(p => { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); });
    GV.cam.x = (x0 + x1) / 2; GV.cam.y = (y0 + y1) / 2;
    GV.cam.k = Math.max(0.15, Math.min(2.2, Math.min(GV.w / (x1 - x0 + 160), GV.h / (y1 - y0 + 120))));
  }
  function graphFocus(id) {
    const p = GV.at[id];
    if (!p) return;
    const s = toScreen(p);
    if (s.x < 60 || s.y < 60 || s.x > GV.w - 60 || s.y > GV.h - 60) { GV.cam.x = p.x; GV.cam.y = p.y; }
  }
  function label(n) {
    const it = C.byId[n.id];
    const t = it.kind === "jira" ? it.id : it.kind === "pr" ? it.id.replace(/^[^/]+\//, "") : it.title;
    return t.length > 34 ? t.slice(0, 33) + "…" : t;
  }
  function graphDraw() {
    const cv = $("#ctxCanvas");
    if (!cv || !GV.colors) return;
    const ctx = cv.getContext("2d"), dpr = window.devicePixelRatio || 1, k = GV.cam.k, c = GV.colors;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, GV.w, GV.h);
    // what to light up: the hovered (or selected) item and its neighbours, like Obsidian's graph
    const focus = GV.hover || (C.filter ? "" : C.sel);    // while filtering, the matches stand out (hover still shows links)
    const near = focus ? new Set([focus].concat(Array.from(adj[focus] || []))) : null;
    const filtering = !!C.filter;
    GV.links.forEach(l => {
      const a = toScreen(l.a), b = toScreen(l.b);
      const on = near && (l.a.id === focus || l.b.id === focus);
      ctx.globalAlpha = near ? (on ? 1 : 0.12) : filtering && !(l.a.hit && l.b.hit) ? 0.15 : 0.7;
      ctx.strokeStyle = on ? c.accent : l.hand ? KCOL.note : c.line;
      ctx.lineWidth = on ? 2 : 1.2;
      ctx.setLineDash(l.hand ? [5, 4] : []);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    });
    ctx.setLineDash([]);
    const labels = [];
    GV.nodes.forEach(n => {
      const s = toScreen(n), r = radius(n) * Math.max(0.6, Math.min(1.6, k));
      if (s.x < -40 || s.y < -40 || s.x > GV.w + 40 || s.y > GV.h + 40) return;
      const lit = near ? near.has(n.id) : !filtering || n.hit;
      ctx.globalAlpha = lit ? 1 : 0.18;
      ctx.fillStyle = KCOL[n.kind];
      ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, Math.PI * 2); ctx.fill();
      if (n.id === C.sel || n.id === GV.hover) { ctx.lineWidth = 2.5; ctx.strokeStyle = c.text; ctx.beginPath(); ctx.arc(s.x, s.y, r + 3, 0, Math.PI * 2); ctx.stroke(); }
      // names fade in as you zoom; hubs, the focus and its neighbours always get one
      const show = (near && near.has(n.id)) || (!near && (filtering ? n.hit : k > 1.15 || n.deg >= 4));
      if (show) labels.push([n, s, r, lit]);
    });
    ctx.font = "12px " + (getComputedStyle(document.body).fontFamily || "sans-serif");
    ctx.textAlign = "center";
    // most important names first (the focus and its neighbours, then hubs); a name that would overlap one already drawn is skipped
    const rank = n => (n.id === focus ? 1e6 : 0) + (near && near.has(n.id) ? 1e5 : 0) + n.deg * 10 + (n.hit && filtering ? 1e4 : 0);
    labels.sort((x, y) => rank(y[0]) - rank(x[0]));
    labels.length = Math.min(labels.length, 400);   // more names than this can't be read anyway
    // what names must not cover: the dots on screen, and names already placed (kept in a coarse grid)
    const G = 48, taken = new Map();
    const cellsOf = b => { const out = []; for (let x = Math.floor(b[0] / G); x <= Math.floor(b[2] / G); x++) for (let y = Math.floor(b[1] / G); y <= Math.floor(b[3] / G); y++) out.push(x + "," + y); return out; };
    const put = b => cellsOf(b).forEach(key => { let l = taken.get(key); if (!l) taken.set(key, (l = [])); l.push(b); });
    const hits = b => cellsOf(b).some(key => (taken.get(key) || []).some(o => b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1]));
    if (labels.length) GV.nodes.forEach(n => {
      if (near && !near.has(n.id)) return;
      const q = toScreen(n), r = radius(n) * Math.max(0.6, Math.min(1.6, k));
      if (q.x > -20 && q.y > -20 && q.x < GV.w + 20 && q.y < GV.h + 20) put([q.x - r, q.y - r, q.x + r, q.y + r]);
    });
    labels.forEach(([n, s, r, lit]) => {
      const t = label(n), w = ctx.measureText(t).width, box = [s.x - w / 2 - 2, s.y + r + 3, s.x + w / 2 + 2, s.y + r + 18];
      if (n.id !== focus && hits(box)) return;
      put(box);
      ctx.globalAlpha = lit ? 1 : 0.3;
      ctx.lineWidth = 3; ctx.strokeStyle = c.bg; ctx.strokeText(t, s.x, s.y + r + 14);
      ctx.fillStyle = n.id === focus ? c.text : c.faint;
      ctx.fillText(t, s.x, s.y + r + 14);
    });
    ctx.globalAlpha = 1;
  }
  function nodeAt(sx, sy) {
    let best = null, bd = Infinity;
    GV.nodes.forEach(n => {
      const s = toScreen(n), d = Math.hypot(s.x - sx, s.y - sy), r = radius(n) * Math.max(0.6, Math.min(1.6, GV.cam.k)) + 4;
      if (d < r && d < bd) { best = n; bd = d; }
    });
    return best;
  }
  function wireGraph(cv) {
    cv.screenOf = id => (GV.at[id] ? toScreen(GV.at[id]) : null);   // where an item is drawn (used by tests)
    const xy = e => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    cv.addEventListener("pointerdown", e => {
      const [x, y] = xy(e), n = nodeAt(x, y);
      cv.setPointerCapture(e.pointerId);
      if (n) { GV.drag = { n, moved: false }; n.fixed = true; }
      else GV.pan = { x, y, cx: GV.cam.x, cy: GV.cam.y, moved: false };
    });
    cv.addEventListener("pointermove", e => {
      const [x, y] = xy(e);
      if (GV.drag) {
        const w = toWorld(x, y);
        GV.drag.n.x = w.x; GV.drag.n.y = w.y; GV.drag.moved = true;
        GV.alpha = Math.max(GV.alpha, 0.25);
        graphRun();
      } else if (GV.pan) {
        if (Math.abs(x - GV.pan.x) + Math.abs(y - GV.pan.y) > 3) GV.pan.moved = true;
        GV.touched = true;
        GV.cam.x = GV.pan.cx - (x - GV.pan.x) / GV.cam.k; GV.cam.y = GV.pan.cy - (y - GV.pan.y) / GV.cam.k;
        graphDraw();
      } else {
        const n = nodeAt(x, y), id = n ? n.id : "";
        cv.style.cursor = n ? "pointer" : "grab";
        if (id !== GV.hover) { GV.hover = id; cv.title = n ? C.byId[id].title : ""; graphDraw(); }
      }
    });
    cv.addEventListener("pointerup", () => {
      if (GV.drag) {
        GV.drag.n.fixed = false;
        if (!GV.drag.moved) select(GV.drag.n.id, true);
        GV.drag = null;
      } else if (GV.pan) {
        if (!GV.pan.moved && C.sel) select(C.sel);      // a click on empty space clears the selection
        GV.pan = null;
      }
    });
    cv.addEventListener("pointerleave", () => { if (GV.hover && !GV.drag) { GV.hover = ""; graphDraw(); } });
    cv.addEventListener("wheel", e => {
      e.preventDefault();
      GV.touched = true;
      const [x, y] = xy(e), before = toWorld(x, y);
      GV.cam.k = Math.max(0.1, Math.min(4, GV.cam.k * Math.exp(-e.deltaY * 0.0015)));
      const after = toWorld(x, y);
      GV.cam.x += before.x - after.x; GV.cam.y += before.y - after.y;
      graphDraw();
    }, { passive: false });
    cv.parentElement.addEventListener("click", e => {
      const b = e.target.closest("[data-gv]");
      if (!b) return;
      if (b.dataset.gv === "fit") graphFit();
      else GV.cam.k = Math.max(0.1, Math.min(4, GV.cam.k * (b.dataset.gv === "in" ? 1.3 : 1 / 1.3)));
      graphDraw();
    });
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
        } else {                   // across a column (Jira ↔ email): around it, along the top of the band
          dashed = true;
          const top = 28, r = 6, x1 = p.r + 10, x2 = q.l - 10;
          d = "M" + p.r + " " + p.y + " H" + (x1 - r) + " Q" + x1 + " " + p.y + " " + x1 + " " + (p.y - r) + " V" + (top + r) + " Q" + x1 + " " + top + " " + (x1 + r) + " " + top +
            " H" + (x2 - r) + " Q" + x2 + " " + top + " " + x2 + " " + (top + r) + " V" + (q.y - r) + " Q" + x2 + " " + q.y + " " + (x2 + r) + " " + q.y + " H" + q.l;
        }
        const cls = (dashed ? "dash " : "") + (isManual(x.id, y.id) ? "hand" : "");
        paths += '<path d="' + d + '" data-a="' + esc(x.id) + '" data-b="' + esc(y.id) + '"' + (cls ? ' class="' + cls.trim() + '"' : "") + "/>";
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
    if (n.more) fullText(n.id);
    const kind = LABEL[n.kind];
    const fields = Object.entries(n.fields || {}).filter(([, v]) => v);
    const linked = Array.from(adj[n.id] || []).map(i => C.byId[i]).filter(Boolean);
    box.innerHTML = '<div class="cd-head"><span class="tag k-' + esc(n.kind) + '">' + kind + '</span><button class="icon-btn" data-a="close" title="Close">' + icon("close") + "</button></div>" +
      "<h3>" + esc(n.title) + '</h3><p class="faint small mono">' + esc(n.kind === "mail" ? (n.fields || {}).from || n.who : n.kind === "note" ? "Added by you" + (n.who && n.who !== "note" ? " · " + n.who : "") : n.id) + "</p>" +
      '<table class="cd-fields">' + (n.state ? "<tr><td>Status</td><td>" + esc(n.state) + "</td></tr>" : "") +
      (n.who && n.kind !== "note" ? "<tr><td>" + (n.kind === "jira" ? "Assignee" : n.kind === "pr" ? "Author" : "From") + "</td><td>" + esc(n.who) + "</td></tr>" : "") +
      (n.updated ? "<tr><td>Updated</td><td>" + esc(new Date(n.updated).toLocaleString()) + "</td></tr>" : "") +
      fields.map(([k, v]) => "<tr><td>" + esc(k.charAt(0).toUpperCase() + k.slice(1)) + "</td><td>" + esc(k === "created" && v ? new Date(v).toLocaleString() : v) + "</td></tr>").join("") + "</table>" +
      '<div class="row tight">' + (n.url ? '<a class="btn small" href="' + esc(n.url) + '" target="_blank" rel="noopener noreferrer">' + icon("external") + "Open</a>" : "") +
      '<button class="btn small primary" data-a="ask">' + icon("spark") + "Ask about this</button>" +
      '<button class="btn small" data-a="linkto" title="Link this to another item by hand">' + icon("link") + "Link to…</button>" +
      (C.mode === "tree" && linked.length ? '<button class="btn small" data-a="reroot" title="Redraw this thread\'s tree starting from this item">Start the tree here</button>' : "") +
      (n.kind === "note" ? '<button class="btn small" data-a="edit">' + icon("edit") + 'Edit</button><button class="icon-btn" data-a="delete" title="Delete this note">' + icon("trash") + "</button>"
        : '<button class="btn small" data-a="addnote" title="Add a note linked to this item">' + icon("plus") + "Note</button>") + "</div>" +
      '<div class="cd-text" id="cdText">' + esc(n.text || "(no description)") + "</div>" +
      (linked.length ? '<h4>Linked (' + linked.length + ")</h4>" + linked.map(m => '<div class="cd-linkrow"><button class="cd-link" data-id="' + esc(m.id) + '"><span class="tag k-' + esc(m.kind) + '">' +
        esc(SHORT[m.kind]) + "</span><span>" + esc(m.kind === "mail" || m.kind === "note" ? m.title : m.id + " · " + m.title) + "</span></button>" +
        (isManual(n.id, m.id) ? '<button class="icon-btn" data-unlink="' + esc(m.id) + '" title="Remove this link (you added it)">' + icon("close") + "</button>" : "") + "</div>").join("")
        : '<p class="faint small">Not linked to anything else. Use <b>Link to…</b> to connect it by hand.</p>');
  }
  // the page gets a short preview of long texts; the detail panel fills in the rest
  async function fullText(id) {
    const n = C.byId[id];
    if (!n || !n.more) return;
    try {
      const it = (await api("GET", "context?item=" + encodeURIComponent(id))).item;
      Object.assign(n, { text: it.text, more: false, _hay: undefined });
      const el = $("#cdText");
      if (el && C.sel === id) el.textContent = it.text || "(no description)";
    } catch (_) { /* keep the preview */ }
  }
  function select(id, keep) {
    C.sel = C.sel === id && !keep ? "" : id;
    if (C.mode === "graph") { if (C.sel) graphFocus(C.sel); graphDraw(); renderDetail(); return; }
    const shown = () => $('#ctxBoard .ccard[data-id="' + CSS.escape(C.sel) + '"]');
    if (C.sel && C.mode !== "graph" && !shown()) {
      reveal(C.sel); render();
      if (!shown()) { reveal(C.sel); render(); }   // the tree's parents are known once its thread is drawn
    }
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
    const what = n.kind === "mail" ? 'the email "' + n.title + '" from ' + n.who : n.kind === "note" ? 'my note "' + n.title + '" (' + n.id + ")" : n.id + ' ("' + n.title + '")';
    $("#prompt").value = "What's the status of " + what + "? Summarize it together with everything linked to it, and tell me what still needs doing.";
    app.autosize();
    $("#prompt").focus();
  }

  // ── context you add by hand ───────────────────────────────────────────
  async function change(body) {
    const r = await api("POST", "context", body);
    await load();
    return r;
  }
  function pickerHTML(exclude, chosen) {
    const items = C.graph.nodes.filter(n => !exclude.has(n.id)).sort((a, b) => (b.updated || "").localeCompare(a.updated || ""));
    return '<input type="search" class="cpick-filter" placeholder="Find an item (ABC-123, a title, a name…)" spellcheck="false"><div class="cpick">' +
      (items.length ? items.map(n => '<label class="check cpick-row" data-hay="' + esc(hay(n)) + '"><input type="checkbox" value="' + esc(n.id) + '"' + (chosen.has(n.id) ? " checked" : "") + '><span class="tag k-' + esc(n.kind) + '">' +
        esc(SHORT[n.kind]) + "</span><span>" + esc(n.kind === "mail" || n.kind === "note" ? n.title : n.id + " · " + n.title) + "</span></label>").join("")
        : '<p class="faint small">Nothing to link to yet.</p>') + "</div>";
  }
  function wirePicker(d) {
    const f = d.querySelector(".cpick-filter");
    f.oninput = () => {
      const words = f.value.toLowerCase().split(/\s+/).filter(Boolean);
      d.querySelectorAll(".cpick-row").forEach(r => (r.hidden = !words.every(w => r.dataset.hay.includes(w))));
    };
    return () => Array.from(d.querySelectorAll(".cpick input:checked")).map(i => i.value);
  }
  function noteDialog(note, linkTo) {
    const own = note ? new Set([note.id]) : new Set();
    const chosen = new Set(note ? Array.from(adj[note.id] || []).filter(j => isManual(note.id, j)) : linkTo ? [linkTo] : []);
    const d = openModal("<h3>" + (note ? "Edit note" : "Add context") + '</h3><p class="faint small">Anything the model should know: meeting notes, a Slack thread, a decision, a document. ' +
      "Mentions of ABC-123 or a PR link automatically; tick items below to link it by hand.</p>" +
      '<label>Title<input id="cnTitle" value="' + esc(note ? note.title : "") + '" placeholder="Standup: webhook retries"></label>' +
      '<div class="grid2"><label>Source (optional)<input id="cnSource" value="' + esc(note && note.who !== "note" ? note.who : "") + '" placeholder="Meeting, Slack, Confluence…"></label>' +
      '<label>Link (optional)<input id="cnUrl" value="' + esc(note ? note.url : "") + '" placeholder="https://…" spellcheck="false"></label></div>' +
      '<label>Text<textarea id="cnText" rows="8" placeholder="What was said or decided…">' + esc(note ? (note.text || "") : "") + "</textarea></label>" +
      "<label>Link to</label>" + pickerHTML(own, chosen) +
      '<p class="small" id="cnMsg"></p><div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">' + (note ? "Save" : "Add") + "</button></div>");
    d.classList.add("wide");
    const picked = wirePicker(d);
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="ok"]').onclick = guard(async () => {
      const links = picked();
      try {
        const r = await change({ action: "note", id: note ? note.id : "", title: $("#cnTitle").value, source: $("#cnSource").value, url: $("#cnUrl").value, text: $("#cnText").value, links });
        if (note) for (const j of Array.from(chosen).filter(x => !links.includes(x))) await change({ action: "unlink", a: note.id, b: j });
        closeModal();
        C.sel = r.note.id;
        render();
        toast(note ? "Note saved." : "Note added.");
      } catch (e) { $("#cnMsg").textContent = e.message; $("#cnMsg").style.color = "var(--red)"; }
    });
    $("#cnTitle").focus();
  }
  function linkDialog(id) {
    const n = C.byId[id];
    const d = openModal("<h3>Link " + esc(n.kind === "jira" || n.kind === "pr" ? n.id : "\u201c" + n.title + "\u201d") + ' to…</h3><p class="faint small">For items that belong together but don\'t mention each other. The link is kept across syncs, and the model sees it.</p>' +
      pickerHTML(new Set([id].concat(Array.from(adj[id] || []))), new Set()) + '<div class="row end"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Link</button></div>');
    d.classList.add("wide");
    const picked = wirePicker(d);
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="ok"]').onclick = guard(async () => {
      for (const other of picked()) await change({ action: "link", a: id, b: other });
      closeModal();
      render();
    });
    d.querySelector(".cpick-filter").focus();
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
      const tog = e.target.closest("[data-tog]");
      if (tog) { const i = tog.dataset.tog; if (C.collapsed.has(i)) C.collapsed.delete(i); else C.collapsed.add(i); render(); return; }
      const sh = e.target.closest("[data-shut]");
      if (sh) { const k = sh.dataset.shut; if (C.shut.has(k)) C.shut.delete(k); else C.shut.add(k); render(); return; }
      const all = e.target.closest("[data-all]");
      if (all) {
        const t = C.graph.threads.find(x => threadKey(x) === all.dataset.key) || [];
        C.shut.delete(all.dataset.key);
        t.forEach(i => (all.dataset.all === "fold" && i !== (C.roots[all.dataset.key] || "") ? C.collapsed.add(i) : C.collapsed.delete(i)));
        if (all.dataset.all === "fold") { const r = buildTree(t).root; C.collapsed.delete(r); }
        render();
        return;
      }
      const ur = e.target.closest("[data-unroot]");
      if (ur) { delete C.roots[ur.dataset.unroot]; render(); return; }
      const jump = e.target.closest("[data-jump]");
      if (jump) { select(jump.dataset.jump, true); return; }
      const c = e.target.closest(".ccard");
      if (c) return select(c.dataset.id);
      if (e.target.closest('[data-a="settings"]')) { app.showView("settings"); const p = $("#ctxSettings"); if (p) p.scrollIntoView(); }
      if (e.target.closest('[data-a="addnote"]')) guard(() => noteDialog(null, ""))();
      if (e.target.closest('[data-a="more"]')) { C.limit += PAGE; render(); }
    });
    board.addEventListener("keydown", e => { const c = e.target.closest(".ccard"); if (c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(c.dataset.id); } });
    board.addEventListener("mouseover", e => { const c = e.target.closest(".ccard"); if (c) highlight(c.dataset.id, true); });
    board.addEventListener("mouseout", e => { const c = e.target.closest(".ccard"); if (c && !c.contains(e.relatedTarget)) highlight(C.sel, !!C.sel); });
    $("#ctxDetail").addEventListener("click", guard(async e => {
      const un = e.target.closest("[data-unlink]");
      if (un) { await change({ action: "unlink", a: C.sel, b: un.dataset.unlink }); render(); return; }
      const b = e.target.closest("[data-a], .cd-link");
      if (!b) return;
      if (b.classList.contains("cd-link")) return select(b.dataset.id);
      if (b.dataset.a === "close") return select(C.sel);
      if (b.dataset.a === "ask") return askAbout(C.byId[C.sel]);
      if (b.dataset.a === "linkto") return linkDialog(C.sel);
      if (b.dataset.a === "reroot") { const t = threadOf(C.sel); if (t) C.roots[threadKey(t)] = C.sel; render(); return; }
      if (b.dataset.a === "addnote") return noteDialog(null, C.sel);
      if (b.dataset.a === "edit") return noteDialog(C.byId[C.sel]);
      if (b.dataset.a === "delete") {
        if (!confirm("Delete the note \u201c" + C.byId[C.sel].title + "\u201d?")) return;
        await change({ action: "delete_note", id: C.sel });
        C.sel = "";
        render();
        return;
      }
    }));
    $("#ctxSync").onclick = guard(sync);
    $("#ctxAdd").onclick = guard(() => noteDialog(null, ""));   // the + Note button on a selected item pre-links it
    $("#ctxLoose").onchange = e => { C.loose = e.target.checked; render(); };
    let t = null;
    $("#ctxFilter").oninput = e => { clearTimeout(t); t = setTimeout(() => { C.filter = e.target.value.trim(); C.limit = PAGE; render(); }, 150); };
    $("#ctxStatus").addEventListener("click", e => {
      if (!e.target.closest("#ctxErrs")) return;
      const d = openModal("<h3>Problems in the last sync</h3><ul>" + C.graph.errors.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>" +
        '<p class="faint small">Fix them in Settings › Work context, then sync again. Everything else that synced is shown.</p><div class="row end"><button class="btn primary" data-a="close">Close</button></div>');
      d.querySelector('[data-a="close"]').onclick = closeModal;
    });
    if (window.ResizeObserver) new ResizeObserver(() => { if (app.S.view !== "context") return; if (C.mode === "columns") drawLinks(); else if (C.mode === "graph") graphResize(); }).observe(board);
    $$("#ctxMode .tab").forEach(t => (t.onclick = () => {
      C.mode = t.dataset.mode;
      try { localStorage.setItem("va-ctx-mode", C.mode); } catch (_) { /* private window */ }
      render();
    }));
  }
  wire();
  return { show, onEvent, load, state: C };
};
