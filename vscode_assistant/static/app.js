// VSCode Assistant, browser version. Talks to the local Python server (server.py).
(function () {
  "use strict";
  const TOKEN = document.querySelector('meta[name="app-token"]').content;
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const esc = MD.esc;
  let Projects = null;   // projects.js, set up in start()
  let Api = null;        // apiview.js
  let Ctx = null;        // contextview.js

  const S = {
    settings: null, chats: [], library: { documents: [], templates: [] }, dataDir: "",
    view: "chat", activeId: null, chat: null, busy: new Set(),
    models: [], images: [], started: {}, libKind: "documents", libCurrent: null, libDirty: false,
    search: "", searchIds: null,
  };

  // ── API ──────────────────────────────────────────────────────────────
  async function api(method, path, body) {
    const r = await fetch("/api/" + path, {
      method, headers: { "X-Token": TOKEN, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty */ }
    if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
    return data;
  }
  const download = path => {
    const a = document.createElement("a");
    a.href = "/api/" + path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
    a.click();
  };

  let toastTimer;
  function toast(text, bad) {
    const t = $("#toast");
    t.textContent = text;
    t.className = "toast" + (bad ? " bad" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), bad ? 5000 : 2600);
  }
  const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

  // ── State ────────────────────────────────────────────────────────────
  async function loadState() {
    const st = await api("GET", "state");
    S.settings = st.settings;
    S.chats = st.chats;
    S.library = st.library;
    S.dataDir = st.data_dir;
    S.busy = new Set(st.chats.filter(c => c.busy).map(c => c.id));
    applyTheme();
  }

  function applyTheme() {
    const t = (S.settings && S.settings.web_theme) || "warm";
    document.documentElement.dataset.theme = t;
    $$("#themeTabs .tab").forEach(b => b.classList.toggle("active", b.dataset.theme === t));
    const sfx = t === "warm" ? "_warm" : "";
    $("#favicon").href = "/assets/icon" + sfx + ".png";
    $$(".logo-small, .logo-greet").forEach(i => (i.src = "/assets/logo_small" + sfx + ".png"));
    $$(".logo-big").forEach(i => (i.src = "/assets/logo" + sfx + ".png"));
  }

  function greeting() {
    const h = new Date().getHours();
    return h < 5 ? "Working late?" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  }

  async function refreshModels() {
    try {
      const r = await api("GET", "models");
      S.models = r.models;
      const def = S.settings.connections.find(c => c.id === S.settings.default_conn) || S.settings.connections[0];
      const off = r.offline.find(o => def && o.conn === def.name);
      setStatus(off ? false : true, off ? def.name + " offline" : (def ? def.name + " · " + r.models.length + " models" : "No servers"));
    } catch (e) {
      setStatus(false, e.message);
    }
    renderModelPicker();
  }
  function setStatus(ok, text) {
    $("#statusDot").className = ok === null ? "" : ok ? "ok" : "bad";
    $("#statusText").textContent = text;
  }
  function currentModel() {
    return (S.chat && S.chat.model) || S.settings.last_model || (S.models[0] && S.models[0].id) || "";
  }

  // ── Events from the server ───────────────────────────────────────────
  function connectEvents() {
    const es = new EventSource("/api/events?token=" + encodeURIComponent(TOKEN));
    es.onmessage = e => handleEvent(JSON.parse(e.data));
    es.onopen = () => { if (S.lostEvents) { S.lostEvents = false; loadState().then(renderRail); } };
    es.onerror = () => { S.lostEvents = true; };
  }

  function handleEvent(ev) {
    if (Projects && (ev.type === "term" || ev.type === "term_done" || ev.type === "chat" || ev.type === "tests")) Projects.onEvent(ev);
    if (Ctx && ev.type === "context") Ctx.onEvent(ev);
    if (Api && ["server", "server_log", "script", "api_progress", "oauth", "rt", "rt_closed"].includes(ev.type)) Api.onEvent(ev);
    if (ev.type === "delta") {
      if (S.chat && ev.chat_id === S.chat.id) {
        S.chat.messages[ev.index] = ev.message;
        paintLive(ev.index);
      }
    } else if (ev.type === "chat") {
      upsertSummary(ev.chat);
      if (ev.chat.busy) { S.busy.add(ev.chat.id); S.started[ev.chat.id] = S.started[ev.chat.id] || Date.now(); }
      else { S.busy.delete(ev.chat.id); delete S.started[ev.chat.id]; }
      if (S.chat && ev.chat.id === S.chat.id) {
        S.chat.messages = ev.messages;
        S.chat.title = ev.chat.title;
        renderChat();
      }
      renderRail();
    } else if (ev.type === "done") {
      S.busy.delete(ev.chat_id);
      if (S.chat && ev.chat_id === S.chat.id) {
        const i = ev.info;
        $("#stats").textContent = i && typeof i === "object" ? statsText(i) : i === "stopped" ? "Stopped" : "";
        updateSend();
      }
      renderRail();
    } else if (ev.type === "confirm") {
      showConfirm(ev);
    } else if (ev.type === "confirm_done" || ev.type === "credential_done") {
      const d = $("#dialog");
      if (d.dataset.confirm === ev.id) closeModal();
    } else if (ev.type === "credential_request") {
      showCredentialRequest(ev);
    } else if (ev.type === "toast") {
      toast(ev.text, ev.bad);
    } else if (ev.type === "pull" || ev.type === "pull_done") {
      pullEvent(ev);
    }
  }

  function upsertSummary(sum) {
    const i = S.chats.findIndex(c => c.id === sum.id);
    if (i >= 0) S.chats[i] = sum; else S.chats.unshift(sum);
    S.chats.sort((a, b) => b.updated - a.updated);
  }

  // ── Rail ─────────────────────────────────────────────────────────────
  // Chats are listed in groups the user names and fills by dragging; "General" holds the rest.
  function groups() { return (S.settings && S.settings.groups) || []; }

  function renderRail() {
    const list = $("#chatList");
    const chats = S.searchIds ? S.chats.filter(c => S.searchIds.has(c.id)) : S.chats;
    const known = new Set(groups().map(g => g.id));
    const item = c => '<div class="chat-item' + (c.id === S.activeId ? " active" : "") + '" draggable="true" data-id="' +
      esc(c.id) + '"><span>' + esc(c.title) + "</span>" + (S.busy.has(c.id) ? '<i class="busy"></i>' : "") +
      '<button class="x" title="Delete" data-del="' + esc(c.id) + '">' + icon("close") + "</button></div>";
    const section = (gid, name, members, collapsed, custom) => {
      if (S.searchIds && !members.length) return "";
      return '<div class="group' + (collapsed ? " collapsed" : "") + '" data-gid="' + esc(gid) + '">' +
        '<div class="group-head" data-gid="' + esc(gid) + '"><span class="gcaret">' + icon(collapsed ? "chevron-right" : "chevron-down") + "</span>" +
        '<span class="gname">' + esc(name) + '</span><span class="gcount">' + members.length + "</span>" +
        '<span class="gacts"><button class="icon-btn" data-gact="new" title="New chat in ' + esc(name) + '">+</button>' +
        (custom ? '<button class="icon-btn" data-gact="rename" title="Rename">' + icon("edit") + "</button>" +
                  '<button class="icon-btn" data-gact="delete" title="Delete group">' + icon("close") + "</button>" : "") + "</span></div>" +
        '<div class="group-body">' + (collapsed ? "" : members.map(item).join("") ||
          '<p class="drop-hint">Drag chats here</p>') + "</div></div>";
    };
    let html = groups().map(g => section(g.id, g.name, chats.filter(c => c.group === g.id), g.collapsed, true)).join("");
    const general = chats.filter(c => !c.group || !known.has(c.group));
    html += section("", "General", general, !!S.generalCollapsed, false);
    if (!chats.length && !groups().length) {
      html = '<p class="faint small" style="padding:8px 20px">' + (S.searchIds ? "No matches." : "No conversations yet.") + "</p>";
    }
    list.innerHTML = html;
  }

  async function saveGroups(gs) {
    await patchSettings({ groups: gs });
    S.chats.forEach(c => { if (c.group && !gs.some(g => g.id === c.group)) c.group = ""; });
    renderRail();
  }

  function renameGroup(gid, isNew) {
    const head = $('#chatList .group-head[data-gid="' + CSS.escape(gid) + '"]');
    const g = groups().find(x => x.id === gid);
    if (!head || !g) return;
    const span = head.querySelector(".gname");
    const inp = document.createElement("input");
    inp.className = "gname-edit";
    inp.value = g.name;
    span.replaceWith(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = guard(async save => {
      if (done) return;
      done = true;
      const name = inp.value.trim();
      if (save && name) await saveGroups(groups().map(x => x.id === gid ? Object.assign({}, x, { name }) : x));
      else if (isNew && !name) await saveGroups(groups().filter(x => x.id !== gid));
      else renderRail();
    });
    inp.onkeydown = e => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); };
    inp.onblur = () => finish(true);
    inp.onclick = e => e.stopPropagation();
  }

  async function moveChat(id, gid) {
    const c = S.chats.find(x => x.id === id);
    if (!c || (c.group || "") === gid) return;
    await api("PATCH", "chats/" + id, { group: gid });
    c.group = gid;
    if (S.chat && S.chat.id === id) S.chat.group = gid;
    const g = groups().find(x => x.id === gid);
    if (g && g.collapsed) await saveGroups(groups().map(x => x.id === gid ? Object.assign({}, x, { collapsed: false }) : x));
    renderRail();
    toast("Moved to " + (g ? g.name : "General"));
  }

  function showView(name) {
    if (S.view === "library" && name !== "library" && !libConfirmLeave()) return;
    if (S.view === "projects" && name !== "projects" && Projects && !Projects.leaving()) return;
    if (S.view === "api" && name !== "api" && Api && !Api.leaving()) return;
    S.view = name;
    $$(".nav a").forEach(a => a.classList.toggle("active", a.dataset.view === name));
    $$(".view").forEach(v => (v.hidden = v.id !== "view-" + name));
    if (name === "models") loadModelsView();
    if (name === "library") loadLibrary();
    if (name === "settings") fillSettings();
    if (name === "passwords") renderVault();
    if (name === "projects") guard(Projects.show)();
    if (name === "api") guard(Api.show)();
    if (name === "context") guard(Ctx.show)();
  }

  // ── Chat ─────────────────────────────────────────────────────────────
  async function openChat(id) {
    if (!id) { S.activeId = null; S.chat = null; renderChat(); renderRail(); return; }
    const r = await api("GET", "chats/" + id);
    S.activeId = id;
    S.chat = r.chat;
    if (r.busy) S.busy.add(id);
    $("#stats").textContent = "";
    renderChat();
    renderRail();
    showView("chat");
    try { localStorage.setItem("va-active", id); } catch (_) { /* private window */ }
  }

  async function newChat(model, group) {
    const r = await api("POST", "chats", { model: model || currentModel() });
    let fast = false;
    try { fast = localStorage.getItem("va-fast") === "1"; } catch (_) { /* private window */ }
    if (group || fast) {
      const patch = {};
      if (group) patch.group = group;
      if (fast) patch.params = { fast: true };
      r.chat = (await api("PATCH", "chats/" + r.chat.id, patch)).chat;
    }
    upsertSummary({ id: r.chat.id, title: r.chat.title, updated: r.chat.updated, model: r.chat.model, group: r.chat.group || "" });
    S.activeId = r.chat.id;
    S.chat = r.chat;
    renderChat();
    renderRail();
    showView("chat");
    $("#prompt").focus();
    return r.chat;
  }

  // "New chat" opens a blank draft; it's only saved as a conversation once you send something or change
  // one of its settings, so pressing New chat again and again doesn't fill the sidebar with empty chats.
  function startDraft(group, model) {
    S.activeId = null;
    S.chat = null;
    S.draft = { group: group || "", model: model || "" };
    $("#stats").textContent = "";
    renderChat();
    renderRail();
    showView("chat");
    $("#prompt").focus();
  }

  async function ensureChat() {
    if (S.chat) return S.chat;
    const d = S.draft || {};
    S.draft = null;
    return await newChat(d.model, d.group);
  }

  function fastDefault() { try { return localStorage.getItem("va-fast") === "1"; } catch (_) { return false; } }

  // the sidebar folds down to a strip of icons (Ctrl+B)
  function setRail(collapsed, save) {
    $("#app").classList.toggle("rail-collapsed", collapsed);
    $("#railToggle").title = collapsed ? "Expand the sidebar (Ctrl+B)" : "Collapse the sidebar (Ctrl+B)";
    if (save) { try { localStorage.setItem("va-rail", collapsed ? "1" : "0"); } catch (_) { /* private window */ } }
  }

  // the + menu in the message box
  function togglePlus(open) {
    const m = $("#plusMenu");
    m.hidden = open === undefined ? !m.hidden : !open;
    $("#plusBtn").classList.toggle("on", !m.hidden);
  }
  async function attachDocs(files) {
    const c = await ensureChat();
    const added = [];
    for (const f of files) {
      const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
      try { added.push((await api("POST", "library/documents/import", { filename: f.name, data })).name); }
      catch (e) { toast(f.name + ": " + e.message, true); }
    }
    if (!added.length) return;
    S.library.documents = Array.from(new Set(S.library.documents.concat(added)));
    await patchChat({ docs: Array.from(new Set((c.docs || []).concat(added))) });
    renderChat();
    toast("The AI can read " + added.join(", ") + " in this chat. It's kept in Library › Documents.");
  }

  function statsText(st) {
    const p = [];
    if (st.tokens) p.push((st.approx ? "~" : "") + st.tokens + " tokens");
    if (st.tps) p.push(st.tps.toFixed(1) + " tok/s");
    if (st.seconds) p.push(st.seconds.toFixed(1) + "s");
    return p.join(" · ");
  }

  function modelName(v) { return v && v.includes("::") ? v.split("::").slice(1).join("::") : (v || "Model"); }

  function renderChat() {
    const c = S.chat;
    $("#chatTitle").textContent = c ? c.title : "New conversation";
    const web = !!(c && c.browser);
    $("#webBtn").textContent = "Web · " + (web ? "on" : "off");
    $("#webBtn").classList.toggle("on", web);
    const pname = c && c.project && Projects ? Projects.projectName(c.project) : "";
    $("#projBtn").textContent = "Project · " + (pname || "none");
    $("#projBtn").classList.toggle("on", !!pname);
    const cx = !!(c && c.context);
    $("#ctxBtn").textContent = "Context · " + (cx ? "on" : "off");
    $("#ctxBtn").classList.toggle("on", cx);
    const ag = !!(c && c.agent);
    $("#agentBtn").textContent = "Browser · " + (ag ? "on" : "off");
    $("#agentBtn").classList.toggle("on", ag);
    const docs = c ? (c.docs || []).filter(d => S.library.documents.includes(d)).length : 0;
    $("#libBtn").textContent = docs ? "Library · " + docs + " doc" + (docs === 1 ? "" : "s") : "Library";
    $("#fastBtn").classList.toggle("on", c ? !!(c.params && c.params.fast) : fastDefault());
    fillParams();
    updateSend();
    const msgs = c ? c.messages : [];
    $("#empty").hidden = msgs.length > 0;
    $("#view-chat").classList.toggle("is-empty", !msgs.length);
    $("#greeting").textContent = greeting();
    const live = c && S.busy.has(c.id);
    $("#messages").innerHTML = msgs.map((m, i) => messageHTML(m, i, live && i === msgs.length - 1)).join("");
    scrollBottom(true);
  }

  function messageHTML(m, i, live) {
    if (m.role === "tool") {
      const args = JSON.stringify(m.arguments || {});
      const st = m.status || (m.is_error ? "error" : "done");
      const res = (m.content || "").trim();
      return '<div class="tool' + (m.is_error ? " err" : "") + '" data-i="' + i + '"><div class="t-head"><span class="name">' + icon("tool") + " " +
        esc(m.name || "tool") + '</span><span class="args">' + esc(args) + '</span><span class="st' +
        (st === "error" || st === "denied" ? " bad" : "") + '">' + (st === "running" ? '<i class="spin"></i>' : "") +
        esc(st) + "</span></div>" +
        (res ? '<div class="res" title="Click to expand">' + esc(res.slice(0, 6000)) + "</div>" : "") +
        (Projects && (m.diff || m.change) ? Projects.toolExtra(m) : "") + "</div>";
    }
    if (m.role === "assistant" && m.tool_calls && !(m.content || "").trim() && !m.thinking) {
      return '<div class="using">→ using tools</div>';
    }
    const user = m.role === "user";
    const who = user ? "You" : modelName(m.model);
    const btn = (act, label) => '<button data-act="' + act + '" data-i="' + i + '" title="' + label + '">' + ICONS[act] +
      '<span class="lbl">' + label + "</span></button>";
    const acts = live ? "" : '<span class="acts">' + btn("copy", "Copy") + (user ? btn("edit", "Edit") : "") +
      (m.role === "assistant" ? btn("regen", "Retry") : "") + btn("delete", "Delete") + "</span>";
    let body;
    if (m.error) body = esc(m.error);
    else if (user) body = esc(m.content || "");
    else body = assistantBody(m, live);
    const imgs = (m.images || []).length ? '<div class="imgs">' + m.images.map(b =>
      '<img src="data:image/png;base64,' + esc(b) + '" alt="">').join("") + "</div>" : "";
    const meta = m.stats ? '<div class="meta">' + esc(statsText(m.stats)) + "</div>" : "";
    return '<div class="msg ' + (user ? "user" : "assistant") + (m.error ? " error" : "") + '" data-i="' + i + '">' +
      '<div class="msg-head"><i class="dot"></i><span>' + esc(who) + "</span></div>" + imgs +
      '<div class="bubble' + (user || m.error ? "" : " md") + '">' + body + "</div>" +
      '<div class="msg-foot">' + acts + meta + "</div></div>";
  }

  const svg = d => '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="' + d + '"/></svg>';
  const ICONS = {
    copy: svg("M5.5 5.5h7v8h-7z M3.5 10.5v-8h7"),
    edit: svg("M10.5 2.5l3 3-7.5 7.5H3v-3z"),
    regen: svg("M13 8a5 5 0 1 1-1.5-3.5M13 2.5v3h-3"),
    delete: svg("M3 4.5h10M6.5 4.5v-2h3v2M4.5 4.5l.7 9h5.6l.7-9"),
  };

  // Shown in a reply until its first words arrive: what the model is doing and for how long.
  function working(label) {
    const id = S.chat ? S.chat.id : "";
    const secs = S.started[id] ? Math.floor((Date.now() - S.started[id]) / 1000) : 0;
    return '<div class="working"><span class="orb"></span><span class="wl">' + esc(label) + '</span>' +
      '<span class="dots"><i></i><i></i><i></i></span><span class="secs">' + secs + "s</span></div>";
  }

  function assistantBody(m, live) {
    const [inlineThink, text, open] = MD.splitThink(m.content || "");
    const think = [(m.thinking || "").trim(), inlineThink.trim()].filter(Boolean).join("\n");
    let h = "";
    if (live && !text.trim()) {
      const fast = S.chat && S.chat.params && S.chat.params.fast;
      h += working(think ? "Thinking" : fast ? "Answering" : "Working");
    }
    if (think) {
      const showOpen = live && (open || !text.trim());
      h += '<details class="think"' + (showOpen ? " open" : "") + "><summary>Reasoning</summary><div>" + esc(think) + "</div></details>";
    }
    h += MD.render(text);
    if (live && text.trim()) h += '<span class="caret"></span>';
    return h;
  }

  let paintPending = false;
  function paintLive(index) {
    if (paintPending) return;
    paintPending = true;
    requestAnimationFrame(() => {
      paintPending = false;
      if (!S.chat || !S.busy.has(S.chat.id)) return;  // finished: renderChat already drew it
      const el = $('#messages .msg[data-i="' + index + '"]');
      const m = S.chat.messages[index];
      if (!el || !m) { renderChat(); return; }
      const stick = nearBottom();
      el.querySelector(".bubble").innerHTML = assistantBody(m, true);
      if (stick) scrollBottom(true);
    });
  }

  function nearBottom() { const t = $("#transcript"); return t.scrollHeight - t.scrollTop - t.clientHeight < 80; }
  function scrollBottom(force) { const t = $("#transcript"); if (force || nearBottom()) t.scrollTop = t.scrollHeight; }

  function updateSend() {
    const busy = S.chat && S.busy.has(S.chat.id);
    const b = $("#sendBtn");
    b.querySelector(".lbl").textContent = busy ? "Stop" : "Send";
    b.title = busy ? "Stop (Esc)" : "Send (Enter)";
    b.classList.toggle("stop", !!busy);
    if (busy) $("#stats").textContent = "Generating…";
  }

  async function send() {
    if (S.chat && S.busy.has(S.chat.id)) { await api("POST", "chats/" + S.chat.id + "/stop", {}); return; }
    const text = $("#prompt").value.trim();
    if (!text && !S.images.length) return;
    const model = currentModel();
    if (!model) { toast("No model selected. Pick one in Settings › Model.", true); return; }
    const c = await ensureChat();
    const images = S.images.map(i => i.data);
    $("#prompt").value = "";
    S.images = [];
    renderAttachments();
    autosize();
    try {
      S.busy.add(c.id);
      S.started[c.id] = Date.now();
      updateSend();
      await api("POST", "chats/" + c.id + "/send", { text, images, model });
    } catch (e) {
      S.busy.delete(c.id);
      updateSend();
      $("#prompt").value = text;
      toast(e.message, true);
    }
  }

  async function messageAction(act, i) {
    const c = S.chat;
    if (!c) return;
    const m = c.messages[i];
    if (act === "copy") {
      await navigator.clipboard.writeText(m.error || m.content || "");
      toast("Copied");
      return;
    }
    if (S.busy.has(c.id)) return;
    if (act === "delete") {
      S.chat = (await api("POST", "chats/" + c.id + "/action", { act: "delete", index: i })).chat;
    } else if (act === "edit") {
      S.chat = (await api("POST", "chats/" + c.id + "/action", { act: "truncate", index: i })).chat;
      $("#prompt").value = m.content || "";
      S.images = (m.images || []).map(b => ({ name: "image", data: b }));
      renderAttachments();
      autosize();
      $("#prompt").focus();
    } else if (act === "regen") {
      S.busy.add(c.id);
      S.started[c.id] = Date.now();
      S.chat = (await api("POST", "chats/" + c.id + "/action", { act: "regen", index: i, model: currentModel() })).chat;
    }
    renderChat();
  }

  async function patchChat(fields) {
    const c = await ensureChat();
    S.chat = (await api("PATCH", "chats/" + c.id, fields)).chat;
    upsertSummary({ id: S.chat.id, title: S.chat.title, updated: S.chat.updated, model: S.chat.model, group: S.chat.group || "" });
    return S.chat;
  }

  function renameChat() {
    if (!S.chat) return;
    const h = $("#chatTitle");
    const inp = document.createElement("input");
    inp.className = "title-edit";
    inp.value = S.chat.title;
    h.replaceWith(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = async save => {
      if (done) return;
      done = true;
      inp.replaceWith(h);
      const t = inp.value.trim();
      if (save && t && t !== S.chat.title) { await patchChat({ title: t }); renderRail(); }
      renderChat();
    };
    inp.onkeydown = e => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); };
    inp.onblur = () => finish(true);
  }

  // parameters drawer
  function fillParams() {
    const p = (S.chat && S.chat.params) || {};
    $("#pSystem").value = p.system || "";
    $("#pTemp").value = p.temperature ?? 0.7;
    $("#pTop").value = p.top_p ?? 0.9;
    $("#pTempV").textContent = (+$("#pTemp").value).toFixed(2);
    $("#pTopV").textContent = (+$("#pTop").value).toFixed(2);
    $("#pCtx").value = p.num_ctx || "";
    $("#pMax").value = p.max_tokens || "";
  }
  let paramTimer;
  function saveParams() {
    $("#pTempV").textContent = (+$("#pTemp").value).toFixed(2);
    $("#pTopV").textContent = (+$("#pTop").value).toFixed(2);
    clearTimeout(paramTimer);
    paramTimer = setTimeout(guard(async () => {
      const num = v => (/^\d+$/.test(v.trim()) ? parseInt(v, 10) : null);
      await patchChat({ params: {
        system: $("#pSystem").value, temperature: +$("#pTemp").value, top_p: +$("#pTop").value,
        num_ctx: num($("#pCtx").value), max_tokens: num($("#pMax").value),
      } });
    }), 400);
  }

  // images
  function addImageFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) continue;
      const r = new FileReader();
      r.onload = () => { S.images.push({ name: f.name || "image", data: String(r.result).split(",")[1] }); renderAttachments(); };
      r.readAsDataURL(f);
    }
  }
  function renderAttachments() {
    $("#attachments").innerHTML = S.images.map((im, i) =>
      '<span class="att"><img src="data:image/png;base64,' + esc(im.data) + '" alt="">' + esc(im.name) +
      '<button type="button" data-rm="' + i + '">' + icon("close") + "</button></span>").join("");
  }
  function autosize() {
    const t = $("#prompt");
    t.style.height = "auto";
    t.style.height = Math.min(Math.max(t.scrollHeight, 52), window.innerHeight * 0.4) + "px";
  }

  // ── Dialogs ──────────────────────────────────────────────────────────
  function openModal(html, opts) {
    const d = $("#dialog");
    d.innerHTML = html;
    d.dataset.confirm = (opts && opts.confirm) || "";
    $("#modal").hidden = false;
    return d;
  }
  function closeModal() { $("#modal").hidden = true; $("#dialog").innerHTML = ""; $("#dialog").dataset.confirm = ""; $("#dialog").className = "dialog"; }

  const AGENT_VERBS = {
    browser_open: "open a page", browser_click: "click something", browser_type: "type into a field",
    browser_select: "choose an option", browser_press: "press a key", browser_back: "go back a page",
    browser_snapshot: "look at the page", browser_scroll: "scroll the page", browser_find: "search the page",
    browser_check: "tick or untick a box", browser_dialog: "answer a pop-up dialog on the page",
  };
  function showConfirm(ev) {
    const pv = Projects && Projects.confirmView(ev);
    if (pv) return showProjectConfirm(ev, pv);
    const args = JSON.stringify(ev.call.arguments || {}, null, 2);
    const agent = ev.call.name in AGENT_VERBS;
    const intro = agent ? "<h3>Allow browser action?</h3><p class=\"dim small\">The model wants to " + esc(AGENT_VERBS[ev.call.name]) +
      " in the agent browser window:</p>" : '<h3>Allow web action?</h3><p class="dim small">The model wants to use the sandbox browser (read-only):</p>';
    const d = openModal(intro +
      '<p><b class="name" style="font-family:var(--mono);color:var(--accent-2)">' + esc(ev.call.name) + "</b></p><pre>" +
      esc(args.slice(0, 4000)) + '</pre><div class="row between"><button class="btn danger" data-a="deny">Deny</button>' +
      '<span><button class="btn" data-a="all">Allow all in this chat</button> <button class="btn primary" data-a="ok">Allow</button></span></div>',
    { confirm: ev.id });
    d.querySelectorAll("button[data-a]").forEach(b => (b.onclick = guard(async () => {
      const a = b.dataset.a;
      closeModal();
      await api("POST", "confirm", { id: ev.id, ok: a !== "deny", all: a === "all" });
    })));
  }

  // A project tool call (a file change, a command, a push…), shown the way it will happen.
  function showProjectConfirm(ev, v) {
    const d = openModal("<h3>" + esc(v.title) + "</h3>" + v.body +
      (v.note ? '<label class="note">If you say no, tell the AI why or what to do instead (optional)<input id="declineNote" placeholder="' +
        (ev.preview.kind === "diff" ? "e.g. keep the old function name" : "e.g. not yet, run the tests first") + '"></label>' : "") +
      '<div class="row between"><button class="btn danger" data-a="deny">' + (v.note ? "Reject" : "Deny") + "</button><span>" +
      (v.allowAll ? '<button class="btn" data-a="all">' + esc(v.allowAll) + "</button> " : "") +
      '<button class="btn primary" data-a="ok">' + esc(v.ok) + "</button></span></div>", { confirm: ev.id });
    if (v.wide) d.classList.add("wide");
    d.querySelectorAll("button[data-a]").forEach(b => (b.onclick = guard(async () => {
      const a = b.dataset.a;
      const prefix = $("#allowPrefix") && $("#allowPrefix").checked ? $("#allowPrefix").value : "";
      const note = $("#declineNote") ? $("#declineNote").value : "";
      closeModal();
      await api("POST", "confirm", { id: ev.id, ok: a !== "deny", all: a === "all", allow_prefix: prefix, note });
    })));
    const ok = d.querySelector('[data-a="ok"]');
    if (ok) setTimeout(() => ok.focus(), 30);
  }

  // ── Saved logins ─────────────────────────────────────────────────────
  // The AI asked to sign in. The user unlocks the vault (if needed), picks a login and approves; only then
  // is that one login sent to the local server, which types it into the page and forgets it.
  function showCredentialRequest(ev) {
    const host = ev.host;
    const fields = (ev.fields || []).join(" and ") || "login";
    const answer = async (ok, it) => {
      closeModal();
      await api("POST", "vault/provide", ok && it ? { id: ev.id, ok: true, site: it.site, username: it.username,
                                                      password: it.password } : { id: ev.id, ok: false });
    };
    const head = '<h3>Use a saved login?</h3><p class="dim small">The AI wants to sign in to <b>' + esc(host) +
      "</b> in the agent browser window and needs your " + esc(fields) + ". It won't see the password; it's " +
      "filled straight into the page, and only on " + esc(host) + ".</p>";
    let d;
    if (!Vault.exists) {
      d = openModal(head + '<p class="small">You haven\'t saved any passwords yet. Add this site on the Passwords page, ' +
        'or sign in yourself in the agent browser window.</p><div class="row end"><button class="btn" data-a="no">Decline</button></div>',
      { confirm: ev.id });
    } else if (!Vault.unlocked) {
      d = openModal(head + '<label>Master password<input type="password" id="credPw" autocomplete="current-password"></label>' +
        '<p class="small" id="credMsg"></p><div class="row between"><button class="btn" data-a="no">Decline</button>' +
        '<button class="btn primary" data-a="unlock">Unlock</button></div>', { confirm: ev.id });
      const go = guard(async () => {
        try { await Vault.unlock($("#credPw").value); showCredentialRequest(ev); }
        catch (e) { $("#credMsg").textContent = e.message; $("#credMsg").style.color = "var(--red)"; }
      });
      d.querySelector('[data-a="unlock"]').onclick = go;
      $("#credPw").onkeydown = e => { if (e.key === "Enter") go(); };
      setTimeout(() => $("#credPw") && $("#credPw").focus(), 50);
    } else {
      const matches = Vault.forHost(host);
      if (matches.length) {
        d = openModal(head + '<div class="opts">' + matches.map((it, i) =>
          '<label class="check"><input type="radio" name="cred" value="' + i + '"' + (i ? "" : " checked") + "> " +
          esc(it.username || "(no username)") + ' <span class="faint">· ' + esc(it.site) + "</span></label>").join("") +
          '</div><div class="row between"><button class="btn" data-a="no">Decline</button>' +
          '<button class="btn primary" data-a="fill">Fill login</button></div>', { confirm: ev.id });
        d.querySelector('[data-a="fill"]').onclick = guard(() =>
          answer(true, matches[+(d.querySelector("input[name=cred]:checked") || {}).value || 0]));
      } else {
        d = openModal(head + '<p class="small">There\'s no saved login for ' + esc(host) + '. Add one now:</p>' +
          '<label>Username or email<input id="credUser" autocomplete="off"></label>' +
          '<label>Password<input type="password" id="credNew" autocomplete="new-password"></label>' +
          '<p class="small" id="credMsg"></p><div class="row between"><button class="btn" data-a="no">Decline</button>' +
          '<button class="btn primary" data-a="add">Save and fill</button></div>', { confirm: ev.id });
        d.querySelector('[data-a="add"]').onclick = guard(async () => {
          try {
            const it = await Vault.save({ site: host, username: $("#credUser").value, password: $("#credNew").value });
            await answer(true, it);
          } catch (e) { $("#credMsg").textContent = e.message; $("#credMsg").style.color = "var(--red)"; }
        });
      }
    }
    d.querySelector('[data-a="no"]').onclick = guard(() => answer(false));
  }

  function renderVault() {
    const ex = Vault.exists, open = Vault.unlocked;
    $("#vaultCreate").hidden = ex;
    $("#vaultUnlock").hidden = !ex || open;
    $("#vaultOpen").hidden = !open;
    $("#vaultLock").hidden = !open;
    $("#vaultState").textContent = !ex ? "No vault yet" : open ? "Unlocked" : "Locked";
    if (!open) return;
    const rows = Vault.list();
    $("#vTable tbody").innerHTML = rows.length ? rows.map(it => "<tr><td>" + esc(it.site) + "</td><td>" +
      esc(it.username) + '</td><td><span class="pw" data-id="' + esc(it.id) + '">••••••••</span></td><td class="acts">' +
      '<button class="btn small" data-v="show" data-id="' + esc(it.id) + '">Show</button>' +
      '<button class="btn small" data-v="copy" data-id="' + esc(it.id) + '">Copy</button>' +
      '<button class="btn small" data-v="edit" data-id="' + esc(it.id) + '">Edit</button>' +
      '<button class="btn small danger" data-v="del" data-id="' + esc(it.id) + '">Delete</button></td></tr>').join("")
      : '<tr class="none"><td colspan="4">No saved logins yet. Add one above.</td></tr>';
  }
  function vaultForm(it) {
    $("#vId").value = it ? it.id : "";
    $("#vSite").value = it ? it.site : "";
    $("#vUser").value = it ? it.username : "";
    $("#vPw").value = it ? it.password : "";
    $("#vPw").type = "password";
    $("#vShow").textContent = "Show";
    $("#vFormTitle").textContent = it ? "Edit login" : "Add a login";
  }

  async function libraryDialog() {
    const c = await ensureChat();
    const st = await api("GET", "state");
    S.library = st.library;
    const docs = S.library.documents, temps = S.library.templates;
    try { const sr = await api("GET", "library/standards"); S.stdCount = sr.names.filter(n => sr.metas[n] && sr.metas[n].active).length; } catch (_) { S.stdCount = 0; }
    const cur = c.template === undefined ? "auto" : c.template;
    const tsel = ["auto", ""].concat(temps).includes(cur) ? cur : "auto";
    const d = openModal('<h3>Documents</h3><p class="faint small">The AI reads the documents you tick, in this chat only.</p><div class="opts">' +
      (docs.length ? docs.map(n => '<label class="check"><input type="checkbox" value="' + esc(n) + '"' +
        ((c.docs || []).includes(n) ? " checked" : "") + "> " + esc(n) + "</label>").join("")
        : '<p class="dim small">No documents yet. Add some on the Library page.</p>') +
      '</div><h3>Template</h3><p class="faint small">Auto lets the AI see every template and use the one that fits the request.</p><div class="opts">' +
      [["auto", "Auto: pick the matching template"], ["", "None"]].concat(temps.map(t => [t, t])).map(([v, l]) =>
        '<label class="check"><input type="radio" name="tpl" value="' + esc(v) + '"' + (v === tsel ? " checked" : "") + "> " + esc(l) + "</label>").join("") +
      '</div>' + (S.stdCount ? '<p class="faint small">' + S.stdCount + " documentation standard" + (S.stdCount === 1 ? " applies" : "s apply") +
        " in every chat (Library › Doc standards).</p>" : "") + '<div class="row between"><button class="btn" data-a="lib">Open Library</button><span><button class="btn" data-a="cancel">Cancel</button> ' +
      '<button class="btn primary" data-a="done">Done</button></span></div>');
    d.querySelector('[data-a="cancel"]').onclick = closeModal;
    d.querySelector('[data-a="lib"]').onclick = () => { closeModal(); showView("library"); };
    d.querySelector('[data-a="done"]').onclick = guard(async () => {
      const picked = Array.from(d.querySelectorAll('input[type=checkbox]:checked')).map(i => i.value);
      const tpl = (d.querySelector('input[name=tpl]:checked') || {}).value ?? "auto";
      closeModal();
      await patchChat({ docs: picked, template: tpl });
      renderChat();
    });
  }

  // ── Models view ──────────────────────────────────────────────────────
  function fmtBytes(n) {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n < 10 && i ? n.toFixed(1) : Math.round(n)) + " " + u[i];
  }
  function ago(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = (Date.now() - t) / 1000;
    return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + "m ago" : s < 86400 ? Math.floor(s / 3600) + "h ago" : Math.floor(s / 86400) + "d ago";
  }
  function until(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = (t - Date.now()) / 1000;
    return s > 1e8 ? "kept loaded" : s <= 0 ? "unloading" : s < 60 ? "unloads in " + Math.round(s) + "s" : "unloads in " + Math.floor(s / 60) + "m";
  }

  function mConn() { return S.settings.connections.find(c => c.id === $("#mConn").value) || S.settings.connections[0]; }

  async function loadModelsView() {
    const sel = $("#mConn");
    const prev = sel.value || S.settings.default_conn;
    sel.innerHTML = S.settings.connections.map(c => '<option value="' + esc(c.id) + '">' + esc(c.name) + "</option>").join("");
    if (S.settings.connections.some(c => c.id === prev)) sel.value = prev;
    const conn = mConn();
    const runBody = $("#runTable tbody"), libBody = $("#libTable tbody");
    if (!conn) { libBody.innerHTML = '<tr class="none"><td colspan="7">Add a server in Settings.</td></tr>'; return; }
    libBody.innerHTML = '<tr class="none"><td colspan="7">Loading…</td></tr>';
    try {
      const r = await api("GET", "models/" + conn.id + "/list");
      $("#pullPanel").hidden = !r.caps.pull;
      $("#runTable").hidden = !r.caps.running;
      $("#runTable").previousElementSibling.hidden = !r.caps.running;
      const loaded = new Set(r.running.map(m => m.name));
      runBody.innerHTML = r.running.length ? r.running.map(m => "<tr><td>" + esc(m.name) + "</td><td>" + esc(m.params || "") +
        "</td><td>" + esc(m.vram != null && m.size ? fmtBytes(m.vram) + " (" + Math.round(100 * m.vram / m.size) + "% GPU)" : fmtBytes(m.size)) +
        "</td><td>" + esc(String(m.ctx || "")) + "</td><td>" + esc(until(m.expires)) + '</td><td class="acts">' +
        '<button class="btn small primary" data-m="chat" data-n="' + esc(m.name) + '">Chat</button>' +
        '<button class="btn small" data-m="unload" data-n="' + esc(m.name) + '">Unload</button></td></tr>').join("")
        : '<tr class="none"><td colspan="6">No models loaded.</td></tr>';
      libBody.innerHTML = r.models.length ? r.models.map(m => "<tr><td>" + esc(m.name) +
        (loaded.has(m.name) ? '<span class="tag">' + icon("dot") + "loaded</span>" : "") + "</td><td>" + esc(m.params || "") + "</td><td>" +
        esc(m.quant || "") + "</td><td>" + esc(m.family || m.owner || "") + "</td><td>" + esc(fmtBytes(m.size)) + "</td><td>" +
        esc(ago(m.modified)) + '</td><td class="acts"><button class="btn small primary" data-m="chat" data-n="' + esc(m.name) + '">Chat</button>' +
        (r.caps.load ? '<button class="btn small" data-m="load" data-n="' + esc(m.name) + '">Load</button>' : "") +
        (r.caps.show ? '<button class="btn small" data-m="show" data-n="' + esc(m.name) + '">Info</button>' : "") +
        (r.caps.remove ? '<button class="btn small danger" data-m="delete" data-n="' + esc(m.name) + '">Delete</button>' : "") +
        "</td></tr>").join("") : '<tr class="none"><td colspan="7">No models installed.</td></tr>';
    } catch (e) {
      runBody.innerHTML = "";
      libBody.innerHTML = '<tr class="none"><td colspan="7">' + esc(e.message) + "</td></tr>";
    }
  }

  async function modelAction(act, name) {
    const conn = mConn();
    const id = conn.id + "::" + name;
    if (act === "chat") {
      await api("PATCH", "settings", { last_model: id });
      S.settings.last_model = id;
      startDraft("", id);
      return;
    }
    if (act === "delete" && !confirm("Delete " + name + " from " + conn.name + "? This removes it from disk.")) return;
    if (act === "show") {
      const r = await api("POST", "models/" + conn.id + "/show", { name });
      const i = r.info || {};
      const txt = ["PARAMETERS\n" + (i.parameters || "(none)"), "TEMPLATE\n" + (i.template || "(none)"),
        "DETAILS\n" + JSON.stringify(i.details || {}, null, 2), "MODEL INFO\n" + JSON.stringify(i.model_info || {}, null, 2),
        "LICENSE\n" + (i.license || "").slice(0, 3000)].join("\n\n");
      const d = openModal("<h3>" + esc(name) + "</h3><pre>" + esc(txt) + '</pre><div class="row end"><button class="btn">Close</button></div>');
      d.querySelector("button").onclick = closeModal;
      return;
    }
    toast((act === "load" ? "Loading " : act === "unload" ? "Unloading " : "Deleting ") + name + "…");
    await api("POST", "models/" + conn.id + "/" + act, { name });
    toast("Done: " + name);
    loadModelsView();
    refreshModels();
  }

  let pulling = null;
  async function pull() {
    const conn = mConn();
    if (pulling) { await api("POST", "models/" + pulling.conn + "/cancel_pull", { name: pulling.name }); return; }
    const name = $("#pullName").value.trim();
    if (!name || !conn) return;
    pulling = { conn: conn.id, name };
    $("#pullBtn").textContent = "Cancel";
    $("#pullBar").hidden = false;
    $("#pullBar").value = 0;
    $("#pullStatus").textContent = "Starting " + name + "…";
    try { await api("POST", "models/" + conn.id + "/pull", { name }); } catch (e) { pullEvent({ type: "pull_done", ok: false, error: e.message, name }); }
  }
  function pullEvent(ev) {
    if (ev.type === "pull") {
      const p = ev.progress;
      if (p.total) $("#pullBar").value = 100 * (p.completed || 0) / p.total;
      $("#pullStatus").textContent = ev.name + ": " + (p.status || "") + (p.total ? "  " + fmtBytes(p.completed || 0) + " / " + fmtBytes(p.total) : "");
    } else {
      pulling = null;
      $("#pullBtn").textContent = "Pull";
      $("#pullBar").hidden = true;
      $("#pullStatus").textContent = ev.ok ? "Pulled " + ev.name + "." : ev.name + ": " + (ev.error || "failed");
      if (ev.ok) { loadModelsView(); refreshModels(); }
    }
  }

  // ── Library view ─────────────────────────────────────────────────────
  const LIB_HINTS = {
    documents: "Reference material the AI can read: notes, specs, policies, code. Choose which documents a chat uses with the Library button in the chat header.",
    templates: "Examples of how answers should look. Use headings, tables and {{placeholders}}; the AI copies the layout and fills in the blanks. In Auto mode (the default) it sees every template and picks the one that fits the request.",
    standards: "Documentation standards the AI must follow whenever it writes documentation, in every chat: same sections, headings and order. Upload your company's templates (Markdown, text or Word) with Import. In a project, a doc file that matches the file patterns is checked: if the AI leaves out a required section, the change is refused and it has to fix it.",
  };
  const LIB_EMPTY = {
    documents: ["art_library", "No documents yet. Add reference material with New or Import, or upload one from the + menu in a chat."],
    templates: ["art_library", "No templates yet. Add an example answer layout with New or Import."],
    standards: ["art_standards", "No documentation standards yet. Import your team's README, API or runbook templates (Markdown, text or Word), or write one with New."],
  };

  async function loadLibrary(select) {
    const kind = S.libKind;
    $$("#view-library .tab").forEach(t => t.classList.toggle("active", t.dataset.kind === kind));
    $("#libHint").textContent = LIB_HINTS[kind];
    const r = await api("GET", "library/" + kind);
    S.library[kind] = r.names;
    $("#libFolder").textContent = "Stored in " + r.folder;
    const label = { documents: "document", templates: "template", standards: "standard" }[kind];
    $("#libCount").textContent = r.names.length + " " + label + (r.names.length === 1 ? "" : "s");
    S.libMetas = r.metas || {};
    let name = select || S.libCurrent;
    if (!r.names.includes(name)) name = r.names[0] || null;
    $("#libItems").innerHTML = r.names.map(n => {
      const off = kind === "standards" && S.libMetas[n] && !S.libMetas[n].active;
      return '<li data-name="' + esc(n) + '" class="' + (n === name ? "active" : "") + (off ? " off" : "") + '"' + (off ? ' title="Off: the AI doesn\'t have to follow it"' : "") +
        ">" + esc(n) + (off ? ' <span class="faint">(off)</span>' : "") + "</li>";
    }).join("");
    const empty = !r.names.length;
    $("#libEmpty").hidden = !empty;
    $$("#view-library .lib-edit > label, #libText, #view-library .lib-edit > .row").forEach(el => (el.hidden = empty));
    if (empty) { $("#libArt").src = "/assets/" + LIB_EMPTY[kind][0] + ".webp"; $("#libEmptyText").textContent = LIB_EMPTY[kind][1]; }
    await libOpen(name);
  }
  async function libOpen(name) {
    S.libCurrent = name;
    $("#libName").value = name || "";
    const got = name ? await api("GET", "library/" + S.libKind + "?name=" + encodeURIComponent(name)) : { text: "" };
    $("#libText").value = got.text;
    $("#stdFields").hidden = S.libKind !== "standards" || !name;
    const m = got.meta || { active: true, use_for: "", files: "" };
    $("#stdActive").checked = m.active;
    $("#stdUse").value = m.use_for;
    $("#stdFiles").value = m.files;
    S.libDirty = false;
    $("#libStatus").textContent = "";
    $$("#libItems li").forEach(li => li.classList.toggle("active", li.dataset.name === name));
  }
  function libConfirmLeave() {
    if (!S.libDirty) return true;
    if (!confirm("You have unsaved changes in the Library. Discard them?")) return false;
    S.libDirty = false;
    return true;
  }
  async function libSave() {
    const name = $("#libName").value.trim();
    if (!name) { $("#libStatus").textContent = "Give it a name."; return; }
    const body = { old: S.libCurrent || "", name, text: $("#libText").value };
    if (S.libKind === "standards") body.meta = { active: $("#stdActive").checked, use_for: $("#stdUse").value, files: $("#stdFiles").value };
    const r = await api("PUT", "library/" + S.libKind, body);
    S.libDirty = false;
    await loadLibrary(r.name);
    $("#libStatus").textContent = "Saved";
  }
  async function libNew() {
    if (!libConfirmLeave()) return;
    const base = { documents: "New document", templates: "New template", standards: "New standard" }[S.libKind];
    const have = new Set(S.library[S.libKind]);
    let name = base + ".md", n = 2;
    while (have.has(name)) name = base + " " + n++ + ".md";
    const r = await api("PUT", "library/" + S.libKind, { name, text: "" });
    await loadLibrary(r.name);
    $("#libText").focus();
  }
  async function libDelete() {
    if (!S.libCurrent || !confirm('Delete "' + S.libCurrent + '" from the library?')) return;
    await api("DELETE", "library/" + S.libKind + "?name=" + encodeURIComponent(S.libCurrent));
    S.libCurrent = null;
    S.libDirty = false;
    await loadLibrary();
  }
  async function libImport(files) {
    if (!libConfirmLeave()) return;
    let last = null;
    for (const f of files) {
      const data = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(String(r.result).split(",")[1] || "");
        r.onerror = rej;
        r.readAsDataURL(f);
      });
      try { last = (await api("POST", "library/" + S.libKind + "/import", { filename: f.name, data })).name; }
      catch (e) { toast(f.name + ": " + e.message, true); }
    }
    if (last) { await loadLibrary(last); toast("Imported " + files.length + " file" + (files.length === 1 ? "" : "s")); }
  }

  // ── Settings view ────────────────────────────────────────────────────
  function renderModelPicker() {
    const sel = $("#sModel");
    const cur = currentModel();
    const opts = S.models.slice();
    if (cur && !opts.some(m => m.id === cur)) opts.unshift({ id: cur, label: modelName(cur) + " (unavailable)" });
    sel.innerHTML = opts.length ? opts.map(m => '<option value="' + esc(m.id) + '">' + esc(m.label) + "</option>").join("")
      : "<option value=''>No models found</option>";
    if (cur) sel.value = cur;
  }
  function fillSettings() {
    const s = S.settings;
    renderModelPicker();
    applyTheme();
    $$("[data-web]").forEach(cb => (cb.checked = s.web[cb.dataset.web] ?? cb.dataset.web !== "allow_private"));
    $("#webSteps").value = s.web.max_steps || 8;
    $("#dSystem").value = s.defaults.system || "";
    $("#dKeep").value = s.defaults.keep_alive || "";
    $("#dataDir").textContent = "Conversations and settings are saved in " + S.dataDir;
    const ag = s.agent || {};
    $("#agentConfirm").value = ag.confirm || "actions";
    const cx = s.context || {};
    $$("[data-ctx]").forEach(el => (el.value = cx[el.dataset.ctx] ?? ""));
    $("#ctxSaved").textContent = "";
    $("#agentSteps").value = ag.max_steps || 25;
    $("#agentChrome").value = ag.chrome_path || "";
    api("GET", "agent/status").then(r => {
      $("#agentStatus").textContent = r.chrome ? (r.running ? "Running · " : "Uses ") + r.chrome : "No Chrome, Edge or Chromium found: set the path above.";
    }).catch(() => {});
    renderConns();
  }
  function renderConns() {
    const s = S.settings;
    $("#connList").innerHTML = s.connections.map(c => '<div class="conn' + (c.id === s.default_conn ? " default" : "") + '"><b>' +
      esc(c.name) + '</b><span class="faint small">' + (c.type === "openai" ? "OpenAI-compatible" : "Ollama") + " · " + esc(c.url) +
      '</span><div class="row">' + (c.id === s.default_conn ? '<span class="tag">default</span>'
        : '<button class="btn small" data-c="default" data-id="' + esc(c.id) + '">Make default</button>') +
      '<button class="btn small" data-c="test" data-id="' + esc(c.id) + '">Test</button><button class="btn small" data-c="edit" data-id="' +
      esc(c.id) + '">Edit</button><button class="btn small danger" data-c="remove" data-id="' + esc(c.id) + '">Remove</button></div></div>').join("");
  }
  function fillConnForm(c) {
    $("#cId").value = c ? c.id : "";
    $("#cName").value = c ? c.name : "";
    $("#cType").value = c ? c.type : "ollama";
    $("#cUrl").value = c ? c.url : "";
    $("#cKey").value = c ? c.key : "";
  }
  async function patchSettings(fields) {
    S.settings = (await api("PATCH", "settings", fields)).settings;
  }

  // ── Wiring ───────────────────────────────────────────────────────────
  function wire() {
    $$(".nav a").forEach(a => (a.onclick = () => showView(a.dataset.view)));
    $("#newChat").onclick = () => startDraft();
    $("#railToggle").onclick = () => setRail(!$("#app").classList.contains("rail-collapsed"), true);
    document.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setRail(!$("#app").classList.contains("rail-collapsed"), true);
      }
    });
    $("#plusBtn").onclick = e => { e.stopPropagation(); togglePlus(); };
    document.addEventListener("click", e => { if (!$("#plusMenu").hidden && !e.target.closest(".plus-wrap")) togglePlus(false); });
    $("#plusMenu").onclick = guard(async e => {
      const b = e.target.closest("[data-plus]");
      if (!b) return;
      togglePlus(false);
      const k = b.dataset.plus;
      if (k === "image") $("#attach").click();
      else if (k === "doc") $("#attachDoc").click();
      else if (k === "library") await libraryDialog();
      else if (k === "project") await Projects.pickForChat();
    });
    $("#attachDoc").onchange = e => { const f = Array.from(e.target.files); e.target.value = ""; guard(attachDocs)(f); };
    $("#chatList").onclick = guard(async e => {
      const del = e.target.closest("[data-del]");
      if (del) {
        const c = S.chats.find(x => x.id === del.dataset.del);
        if (!confirm('Delete "' + (c ? c.title : "this chat") + '"?')) return;
        await api("DELETE", "chats/" + del.dataset.del);
        S.chats = S.chats.filter(x => x.id !== del.dataset.del);
        if (S.activeId === del.dataset.del) await openChat(S.chats[0] && S.chats[0].id);
        renderRail();
        return;
      }
      const gact = e.target.closest("[data-gact]");
      if (gact) {
        e.stopPropagation();
        const gid = gact.closest(".group-head").dataset.gid;
        const g = groups().find(x => x.id === gid);
        if (gact.dataset.gact === "new") startDraft(gid);
        else if (gact.dataset.gact === "rename") renameGroup(gid);
        else if (gact.dataset.gact === "delete" && g &&
                 confirm('Delete the group "' + g.name + '"? Its chats move to General.')) {
          await saveGroups(groups().filter(x => x.id !== gid));
        }
        return;
      }
      const head = e.target.closest(".group-head");
      if (head) {
        const gid = head.dataset.gid;
        if (!gid) { S.generalCollapsed = !S.generalCollapsed; renderRail(); return; }
        await saveGroups(groups().map(x => x.id === gid ? Object.assign({}, x, { collapsed: !x.collapsed }) : x));
        return;
      }
      const item = e.target.closest(".chat-item");
      if (item) await openChat(item.dataset.id);
    });
    $("#chatList").ondblclick = e => {
      const head = e.target.closest(".group-head");
      if (head && head.dataset.gid && e.target.closest(".gname")) renameGroup(head.dataset.gid);
    };
    $("#newGroup").onclick = guard(async () => {
      const id = "g" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      await saveGroups(groups().concat([{ id, name: "New group", collapsed: false }]));
      renameGroup(id, true);
    });
    // drag a chat onto a group (its header or its list) to move it there
    const rail = $("#chatList");
    rail.addEventListener("dragstart", e => {
      const it = e.target.closest && e.target.closest(".chat-item");
      if (!it) return;
      e.dataTransfer.setData("text/x-chat", it.dataset.id);
      e.dataTransfer.effectAllowed = "move";
      it.classList.add("dragging");
    });
    rail.addEventListener("dragend", () => $$(".dragging, .drop-on").forEach(x => x.classList.remove("dragging", "drop-on")));
    rail.addEventListener("dragover", e => {
      const grp = e.target.closest(".group");
      if (!grp || !e.dataTransfer.types.includes("text/x-chat")) return;
      e.preventDefault();
      $$(".drop-on").forEach(x => x !== grp && x.classList.remove("drop-on"));
      grp.classList.add("drop-on");
    });
    rail.addEventListener("dragleave", e => {
      const grp = e.target.closest(".group");
      if (grp && !grp.contains(e.relatedTarget)) grp.classList.remove("drop-on");
    });
    rail.addEventListener("drop", e => {
      const grp = e.target.closest(".group");
      const id = e.dataTransfer.getData("text/x-chat");
      $$(".drop-on").forEach(x => x.classList.remove("drop-on"));
      if (!grp || !id) return;
      e.preventDefault();
      guard(moveChat)(id, grp.dataset.gid);
    });
    let searchTimer;
    $("#search").oninput = () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(guard(async () => {
        const q = $("#search").value.trim();
        S.searchIds = q ? new Set((await api("GET", "search?q=" + encodeURIComponent(q))).ids) : null;
        renderRail();
      }), 200);
    };

    // chat header
    $("#chatTitle").onclick = renameChat;
    $("#webBtn").onclick = guard(async () => {
      const c = await patchChat({ browser: !(S.chat && S.chat.browser) });
      renderChat();
      toast(c.browser ? "Web sandbox on: the model can search and read pages (read-only)." : "Web sandbox off for this chat.");
    });
    $("#libBtn").onclick = guard(libraryDialog);
    $("#projBtn").onclick = guard(() => Projects.pickForChat());
    $("#fastBtn").onclick = guard(async () => {
      const c = await ensureChat();
      const on = !(c.params && c.params.fast);
      await patchChat({ params: { fast: on } });
      try { localStorage.setItem("va-fast", on ? "1" : "0"); } catch (_) { /* private window */ }
      renderChat();
      toast(on ? "Fast mode on: the model skips its reasoning step and answers straight away."
               : "Fast mode off: reasoning models think before answering.");
    });
    $("#ctxBtn").onclick = guard(async () => {
      const c = await patchChat({ context: !(S.chat && S.chat.context) });
      renderChat();
      toast(c.context ? "Context on: the model can read your synced pull requests, Jira issues and email (the saved copy; nothing goes online)."
        : "Context off for this chat.");
    });
    const saveCtx = async () => {
      const v = {};
      $$("[data-ctx]").forEach(el => (v[el.dataset.ctx] = el.value));
      await patchSettings({ context: v });
      fillSettings();
    };
    $("#ctxSave").onclick = guard(async () => { await saveCtx(); $("#ctxSaved").textContent = "Saved."; });
    $("#ctxSaveSync").onclick = guard(async () => {
      await saveCtx();
      await api("POST", "context", { action: "sync" });
      showView("context");
    });
    $("#agentBtn").onclick = guard(async () => {
      const c = await patchChat({ agent: !(S.chat && S.chat.agent) });
      renderChat();
      toast(c.agent ? "Browser on: the model can open pages, click and type in its own browser window. It asks before each action."
        : "Browser off for this chat.");
    });
    $("#paramsBtn").onclick = () => ($("#params").hidden = !$("#params").hidden);
    $("#exportBtn").onclick = () => {
      if (!S.chat || !S.chat.messages.length) { toast("Nothing to export yet."); return; }
      download("chats/" + S.chat.id + "/export");
    };
    ["#pSystem", "#pTemp", "#pTop", "#pCtx", "#pMax"].forEach(s => ($(s).oninput = saveParams));

    // messages
    $("#messages").onclick = guard(async e => {
      const b = e.target.closest("[data-act]");
      if (b) return messageAction(b.dataset.act, +b.dataset.i);
      const cp = e.target.closest("[data-copy]");
      if (cp) {
        await navigator.clipboard.writeText(cp.closest("pre").querySelector("code").textContent);
        cp.textContent = "Copied";
        setTimeout(() => (cp.textContent = "Copy"), 1200);
        return;
      }
      const un = e.target.closest("[data-undo]");
      if (un) {
        const m = S.chat.messages[+un.closest("[data-i]").dataset.i];
        await Projects.undo(m.project || S.chat.project, un.dataset.undo);
        un.disabled = true;
        un.textContent = "Undone";
        return;
      }
      const res = e.target.closest(".tool .res");
      if (res) res.classList.toggle("open");
    });

    // composer
    $("#composer").onsubmit = e => { e.preventDefault(); guard(send)(); };
    $("#prompt").onkeydown = e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); guard(send)(); }
    };
    $("#prompt").oninput = autosize;
    $("#prompt").onpaste = e => {
      const files = Array.from(e.clipboardData.files || []).filter(f => f.type.startsWith("image/"));
      if (files.length) { e.preventDefault(); addImageFiles(files); }
    };
    $("#attach").onchange = e => { addImageFiles(e.target.files); e.target.value = ""; };
    $("#attachments").onclick = e => {
      const b = e.target.closest("[data-rm]");
      if (b) { S.images.splice(+b.dataset.rm, 1); renderAttachments(); }
    };
    const comp = $("#composer");
    comp.ondragover = e => { e.preventDefault(); comp.classList.add("drag"); };
    comp.ondragleave = () => comp.classList.remove("drag");
    comp.ondrop = e => { e.preventDefault(); comp.classList.remove("drag"); addImageFiles(e.dataTransfer.files); };
    document.addEventListener("keydown", e => {
      if (e.key === "Escape") {
        if (!$("#plusMenu").hidden) { togglePlus(false); return; }
        if (!$("#modal").hidden && !$("#dialog").dataset.confirm) { closeModal(); return; }
        if (S.chat && S.busy.has(S.chat.id)) api("POST", "chats/" + S.chat.id + "/stop", {});
      }
    });

    // models
    $("#mConn").onchange = loadModelsView;
    $("#mRefresh").onclick = () => { loadModelsView(); refreshModels(); };
    $("#pullBtn").onclick = guard(pull);
    $("#pullName").onkeydown = e => { if (e.key === "Enter") guard(pull)(); };
    $("#view-models").onclick = guard(async e => {
      const b = e.target.closest("[data-m]");
      if (b) await modelAction(b.dataset.m, b.dataset.n);
    });

    // library
    $$("#view-library .tab").forEach(t => (t.onclick = guard(async () => {
      if (t.dataset.kind === S.libKind || !libConfirmLeave()) return;
      S.libKind = t.dataset.kind;
      S.libCurrent = null;
      await loadLibrary();
    })));
    $("#libItems").onclick = guard(async e => {
      const li = e.target.closest("li");
      if (li && li.dataset.name !== S.libCurrent && libConfirmLeave()) await libOpen(li.dataset.name);
    });
    $("#libName").oninput = $("#libText").oninput = $("#stdUse").oninput = $("#stdFiles").oninput = $("#stdActive").onchange =
      () => { S.libDirty = true; $("#libStatus").textContent = "Unsaved changes"; };
    $("#libText").onkeydown = e => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); guard(libSave)(); } };
    $("#libSave").onclick = guard(libSave);
    $("#libNew").onclick = guard(libNew);
    $("#libDelete").onclick = guard(libDelete);
    $("#libImport").onchange = e => { const f = Array.from(e.target.files); e.target.value = ""; guard(libImport)(f); };

    // passwords
    Vault.onChange(() => { if (S.view === "passwords") renderVault(); });
    const msg = (el, text, bad) => { $(el).textContent = text; $(el).style.color = bad ? "var(--red)" : "var(--green)"; };
    $("#vCreate").onclick = guard(async () => {
      if ($("#vNew1").value !== $("#vNew2").value) return msg("#vCreateMsg", "The two passwords don't match.", true);
      try { await Vault.create($("#vNew1").value); $("#vNew1").value = $("#vNew2").value = ""; }
      catch (e) { msg("#vCreateMsg", e.message, true); }
    });
    const unlock = guard(async () => {
      msg("#vUnlockMsg", "Unlocking…");
      try { await Vault.unlock($("#vPass").value); $("#vPass").value = ""; msg("#vUnlockMsg", ""); }
      catch (e) { msg("#vUnlockMsg", e.message, true); }
    });
    $("#vUnlock").onclick = unlock;
    $("#vPass").onkeydown = e => { if (e.key === "Enter") unlock(); };
    $("#vReset").onclick = guard(async () => {
      if (!confirm("Delete the whole password vault? Every saved login in it is lost. This can't be undone.")) return;
      await Vault.destroy();
      toast("Vault deleted");
    });
    $("#vaultLock").onclick = () => { Vault.lock(); toast("Passwords locked"); };
    $("#vShow").onclick = () => { const p = $("#vPw"); p.type = p.type === "password" ? "text" : "password";
      $("#vShow").textContent = p.type === "password" ? "Show" : "Hide"; };
    $("#vGen").onclick = () => { $("#vPw").value = Vault.generate(20); $("#vPw").type = "text"; $("#vShow").textContent = "Hide"; };
    $("#vClear").onclick = () => vaultForm(null);
    $("#vSave").onclick = guard(async () => {
      await Vault.save({ id: $("#vId").value, site: $("#vSite").value, username: $("#vUser").value, password: $("#vPw").value });
      vaultForm(null);
      toast("Login saved");
    });
    $("#vTable").onclick = guard(async e => {
      const b = e.target.closest("[data-v]");
      if (!b) return;
      const it = Vault.list().find(x => x.id === b.dataset.id);
      if (!it) return;
      if (b.dataset.v === "show") {
        const span = b.closest("tr").querySelector(".pw");
        const showing = b.textContent === "Hide";
        span.textContent = showing ? "••••••••" : it.password;
        b.textContent = showing ? "Show" : "Hide";
      } else if (b.dataset.v === "copy") {
        await navigator.clipboard.writeText(it.password);
        toast("Password copied");
      } else if (b.dataset.v === "edit") {
        vaultForm(it);
        $("#vSite").focus();
      } else if (b.dataset.v === "del" && confirm("Delete the login for " + it.site + "?")) {
        await Vault.remove(it.id);
      }
    });
    $("#vChange").onclick = guard(async () => {
      if ($("#vChg1").value !== $("#vChg2").value) return msg("#vChangeMsg", "The two passwords don't match.", true);
      try { await Vault.changePassword($("#vChg1").value); $("#vChg1").value = $("#vChg2").value = "";
        msg("#vChangeMsg", "Master password changed."); }
      catch (e) { msg("#vChangeMsg", e.message, true); }
    });

    // settings
    $("#sModel").onchange = guard(async () => {
      const v = $("#sModel").value;
      await patchSettings({ last_model: v });
      if (S.chat) await patchChat({ model: v });
    });
    $$("#themeTabs .tab").forEach(t => (t.onclick = guard(async () => { await patchSettings({ web_theme: t.dataset.theme }); applyTheme(); })));
    $$("[data-web]").forEach(cb => (cb.onchange = guard(() => patchSettings({ web: { [cb.dataset.web]: cb.checked } }))));
    $("#webSteps").onchange = guard(() => patchSettings({ web: { max_steps: parseInt($("#webSteps").value, 10) || 8 } }));
    $("#agentConfirm").onchange = guard(() => patchSettings({ agent: { confirm: $("#agentConfirm").value } }));
    $("#agentSteps").onchange = guard(() => patchSettings({ agent: { max_steps: parseInt($("#agentSteps").value, 10) || 25 } }));
    $("#agentChrome").onchange = guard(async () => { await patchSettings({ agent: { chrome_path: $("#agentChrome").value } }); fillSettings(); });
    $("#agentOpen").onclick = guard(async () => {
      $("#agentStatus").textContent = "Starting…";
      await api("POST", "agent/open", {});
      toast("Agent browser opened. Log in to any sites you want it to use; the window remembers them.");
      fillSettings();
    });
    $("#dSystem").onchange = guard(() => patchSettings({ defaults: { system: $("#dSystem").value } }));
    $("#dKeep").onchange = guard(() => patchSettings({ defaults: { keep_alive: $("#dKeep").value.trim() } }));
    $("#connList").onclick = guard(async e => {
      const b = e.target.closest("[data-c]");
      if (!b) return;
      const c = S.settings.connections.find(x => x.id === b.dataset.id);
      if (b.dataset.c === "edit") fillConnForm(c);
      else if (b.dataset.c === "default") { await patchSettings({ default_conn: c.id }); renderConns(); refreshModels(); }
      else if (b.dataset.c === "test") toast(c.name + ": " + (await api("GET", "models/" + c.id + "/ping")).text);
      else if (b.dataset.c === "remove" && confirm("Remove " + c.name + "? Chats that used it keep their history.")) {
        S.settings = (await api("DELETE", "connections/" + c.id)).settings;
        renderConns();
        refreshModels();
      }
    });
    $("#connForm").onsubmit = guard(async e => {
      e.preventDefault();
      S.settings = (await api("POST", "connections", { id: $("#cId").value, name: $("#cName").value, type: $("#cType").value,
        url: $("#cUrl").value, key: $("#cKey").value })).settings;
      fillConnForm(null);
      renderConns();
      refreshModels();
      toast("Server saved");
    });
    $("#cClear").onclick = () => fillConnForm(null);
    $("#backupBtn").onclick = () => download("backup");
    $("#restore").onchange = guard(async e => {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      const r = await api("POST", "backup", JSON.parse(await f.text()));
      await loadState();
      renderRail();
      toast("Imported " + r.added + " conversation" + (r.added === 1 ? "" : "s"));
    });
    $("#wipeBtn").onclick = guard(async () => {
      if (!confirm("Delete every conversation? This cannot be undone.")) return;
      await api("DELETE", "chats");
      S.chats = [];
      await openChat(null);
    });

    window.addEventListener("beforeunload", e => { if (S.libDirty) { e.preventDefault(); e.returnValue = ""; } });
  }

  // ── Start ────────────────────────────────────────────────────────────
  (async function start() {
    let railSaved = null;
    try { railSaved = localStorage.getItem("va-rail"); } catch (_) { /* private window */ }
    setRail(railSaved === "1" || (railSaved === null && window.innerWidth < 900), false);
    Projects = window.VAProjects({ api, toast, guard, esc, openModal, closeModal, $, $$, S, showView, patchChat,
      renderChat, renderRail, newChat, currentModel, autosize });
    Ctx = window.VAContext({ api, toast, guard, esc, openModal, closeModal, $, $$, S, showView, patchChat, renderChat,
      renderRail, newChat, autosize });
    Api = window.VAApi({ api, toast, guard, esc, openModal, closeModal, $, $$, S, showView, patchChat, renderChat,
      renderRail, newChat, currentModel, autosize, download, Projects });
    wire();
    try {
      await loadState();
    } catch (e) {
      toast(e.message, true);
      return;
    }
    await guard(() => Vault.init(api))();
    await guard(Projects.load)();
    connectEvents();
    renderRail();
    let last = null;
    try { last = localStorage.getItem("va-active"); } catch (_) { /* private window */ }
    const first = S.chats.find(c => c.id === last) || S.chats[0];
    await guard(openChat)(first && first.id);
    refreshModels();
    setInterval(refreshModels, 60000);
    setInterval(() => {   // keep the "Thinking… 12s" counters ticking
      const t0 = S.chat && S.started[S.chat.id];
      if (t0) $$("#messages .working .secs").forEach(el => (el.textContent = Math.floor((Date.now() - t0) / 1000) + "s"));
    }, 250);
  })();
})();
