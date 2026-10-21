// The code editor for Projects: Monaco (the editor VS Code is built on), loaded from static/vendor/monaco,
// with one model per open file so each keeps its own undo history and unsaved edits. If Monaco can't load,
// a plain textarea with line numbers and auto-indent stands in, so the app never loses its editor.
// VAEditor(host) returns { ready, open, close, value, setValue, focus, reveal, ... }; projects.js uses it.
window.VAEditor = function (host, opts) {
  "use strict";
  opts = opts || {};
  const BASE = "/static/vendor/monaco";
  const E = { monaco: null, editor: null, models: new Map(), key: null, listeners: { change: [], save: [], cursor: [] }, fallback: null };
  const emit = (name, ...a) => E.listeners[name].forEach(fn => fn(...a));

  const LANG = {
    java: "java", kt: "kotlin", kts: "kotlin", scala: "scala", groovy: "java", gradle: "java",
    xml: "xml", pom: "xml", fxml: "xml", form: "xml", xsd: "xml", wsdl: "xml", xhtml: "html", jsp: "html", tld: "xml",
    properties: "ini", ini: "ini", cfg: "ini", conf: "ini", toml: "ini", env: "ini",
    json: "json", yml: "yaml", yaml: "yaml", md: "markdown", markdown: "markdown",
    js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
    py: "python", rb: "ruby", go: "go", rs: "rust", c: "c", h: "cpp", cpp: "cpp", cc: "cpp", hpp: "cpp", cs: "csharp",
    php: "php", swift: "swift", sql: "sql", html: "html", htm: "html", css: "css", scss: "scss", less: "less",
    sh: "shell", bash: "shell", zsh: "shell", bat: "bat", cmd: "bat", ps1: "powershell", r: "r", lua: "lua", dart: "dart",
    dockerfile: "dockerfile", tf: "hcl", hcl: "hcl", graphql: "graphql", proto: "protobuf", vue: "html",
  };
  function language(path) {
    const name = path.split("/").pop().toLowerCase();
    if (name === "dockerfile" || name.startsWith("dockerfile.")) return "dockerfile";
    if (name === "makefile") return "shell";
    const ext = name.includes(".") ? name.split(".").pop() : "";
    return LANG[ext] || "plaintext";
  }

  // ── colours from the app's theme ─────────────────────────────────────
  function theme() {
    const m = E.monaco;
    if (!m) return;
    const css = getComputedStyle(document.documentElement);
    const v = (k, d) => (css.getPropertyValue(k).trim() || d);
    const light = (document.documentElement.dataset.theme === "light") ||
      (document.documentElement.dataset.theme === "system" && window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches);
    const hex = c => (/^#[0-9a-f]{6}$/i.test(c) ? c : light ? "#ffffff" : "#1e1e1e");
    m.editor.defineTheme("va", {
      base: light ? "vs" : "vs-dark", inherit: true, rules: [],
      colors: {
        "editor.background": hex(v("--code", "")), "editorGutter.background": hex(v("--code", "")),
        "editor.lineHighlightBackground": light ? "#00000008" : "#ffffff08",
        "editorLineNumber.foreground": hex(v("--faint", "")), "editorLineNumber.activeForeground": hex(v("--dim", "")),
        "editorWidget.background": hex(v("--panel", "")), "editorWidget.border": hex(v("--line-2", "")),
        "editorSuggestWidget.background": hex(v("--panel", "")), "editorSuggestWidget.border": hex(v("--line-2", "")),
        "editorHoverWidget.background": hex(v("--panel", "")), "editorHoverWidget.border": hex(v("--line-2", "")),
        "focusBorder": hex(v("--accent", "")), "editorCursor.foreground": hex(v("--accent", "")),
      },
    });
    m.editor.setTheme("va");
  }

  // ── Monaco ───────────────────────────────────────────────────────────
  function loadMonaco() {
    return new Promise(resolve => {
      if (!window.require || !window.require.config) return resolve(false);
      // workers come from a small file next to Monaco (the page's security policy allows only its own scripts)
      window.MonacoEnvironment = { getWorkerUrl: () => BASE + "/worker.js" };
      window.require.config({ paths: { vs: BASE + "/vs" } });
      const t = setTimeout(() => resolve(false), 20000);
      window.require(["vs/editor/editor.main"], () => { clearTimeout(t); resolve(true); }, () => { clearTimeout(t); resolve(false); });
    });
  }
  function startMonaco() {
    const m = E.monaco = window.monaco;
    theme();
    new MutationObserver(theme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    if (window.matchMedia) matchMedia("(prefers-color-scheme: light)").addEventListener("change", theme);
    host.innerHTML = "";
    E.editor = m.editor.create(host, {
      model: null, automaticLayout: true, fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace",
      fontSize: 13, lineHeight: 20, minimap: { enabled: true, renderCharacters: false, maxColumn: 80 }, scrollBeyondLastLine: false,
      renderWhitespace: "selection", bracketPairColorization: { enabled: true }, guides: { bracketPairs: "active", indentation: true },
      smoothScrolling: true, glyphMargin: true, folding: true, stickyScroll: { enabled: true }, tabSize: 4, detectIndentation: true,
      fixedOverflowWidgets: true, padding: { top: 6 },
    });
    E.editor.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyS, () => emit("save"));
    E.editor.onDidChangeCursorPosition(e => emit("cursor", e.position));
  }
  function modelFor(key, path, text, readOnly, lang) {
    let entry = E.models.get(key);
    if (!entry) {
      const uri = api.uriOf(key);
      const model = E.monaco.editor.getModel(uri) || E.monaco.editor.createModel(text, lang || language(path), uri);
      entry = { model, path, saved: text, readOnly: !!readOnly, view: null };
      model.onDidChangeContent(() => emit("change", key, model.getValue() !== entry.saved));
      E.models.set(key, entry);
    }
    return entry;
  }

  // ── the textarea stand-in ────────────────────────────────────────────
  function startFallback() {
    host.innerHTML = '<div class="ed-code"><pre class="gutter"></pre><textarea spellcheck="false" wrap="off"></textarea></div>';
    const ta = host.querySelector("textarea"), gut = host.querySelector(".gutter");
    const gutter = () => { const n = ta.value.split("\n").length; let s = ""; for (let i = 1; i <= n; i++) s += i + "\n"; gut.textContent = s; gut.scrollTop = ta.scrollTop; };
    const unitOf = text => {
      const lines = text.split("\n"), tabs = lines.filter(l => l.startsWith("\t")).length;
      const two = lines.filter(l => /^ {2}\S/.test(l)).length, four = lines.filter(l => /^ {4}\S/.test(l)).length;
      return tabs > two + four ? "\t" : two > four ? "  " : "    ";
    };
    ta.oninput = () => { const e = E.models.get(E.key); if (e) { e.text = ta.value; emit("change", E.key, ta.value !== e.saved); } gutter(); };
    ta.onscroll = () => (gut.scrollTop = ta.scrollTop);
    ta.onkeydown = e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); emit("save"); return; }
      if (e.key === "Tab" && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        const unit = unitOf(ta.value), s = ta.selectionStart, en = ta.selectionEnd, v = ta.value;
        if (s === en && !e.shiftKey) { document.execCommand("insertText", false, unit); return; }
        const ls = v.lastIndexOf("\n", s - 1) + 1, block = v.slice(ls, en);
        const changed = e.shiftKey ? block.replace(new RegExp("^(\\t| {1," + unit.length + "})", "gm"), "") : block.replace(/^/gm, unit);
        ta.setSelectionRange(ls, en);
        document.execCommand("insertText", false, changed);
        ta.setSelectionRange(ls, ls + changed.length);
      } else if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) {
        const s = ta.selectionStart, v = ta.value, line = v.slice(v.lastIndexOf("\n", s - 1) + 1, s);
        let ind = (/^[ \t]*/.exec(line) || [""])[0];
        if (/[:{[(]\s*$/.test(line)) ind += unitOf(v);
        e.preventDefault();
        document.execCommand("insertText", false, "\n" + ind);
      }
    };
    E.fallback = { ta, gutter };
  }

  const ready = (opts.plain ? Promise.resolve(false) : loadMonaco()).then(ok => {
    if (ok && window.monaco) startMonaco(); else startFallback();
    return !!E.monaco;
  });

  // ── what projects.js uses ────────────────────────────────────────────
  const api = {
    ready,
    get monaco() { return E.monaco; },
    get editor() { return E.editor; },
    get key() { return E.key; },
    language,
    on(name, fn) { E.listeners[name].push(fn); },
    has(key) { return E.models.has(key); },
    // show a file; keeps its model (and undo history) if it was open before
    open(key, path, text, o) {
      o = o || {};
      if (E.monaco) {
        const prev = E.key && E.models.get(E.key);
        if (prev && prev.model) prev.view = E.editor.saveViewState();
        const entry = modelFor(key, path, text, o.readOnly, o.language);
        E.key = key;
        E.editor.setModel(entry.model);
        E.editor.updateOptions({ readOnly: !!entry.readOnly });
        if (entry.view) E.editor.restoreViewState(entry.view);
        return;
      }
      let entry = E.models.get(key);
      if (!entry) { entry = { path, saved: text, text, readOnly: !!o.readOnly }; E.models.set(key, entry); }
      E.key = key;
      E.fallback.ta.readOnly = !!entry.readOnly;
      E.fallback.ta.value = entry.text;
      E.fallback.gutter();
    },
    close(key) {
      const entry = E.models.get(key);
      if (!entry) return;
      if (entry.model) entry.model.dispose();
      E.models.delete(key);
      if (E.key === key) { E.key = null; if (E.editor) E.editor.setModel(null); else E.fallback.ta.value = ""; }
    },
    value(key) {
      const entry = E.models.get(key || E.key);
      if (!entry) return null;
      return entry.model ? entry.model.getValue() : entry.text;
    },
    // replace the text (a reload from disk); undo still works
    setValue(key, text, asSaved) {
      const entry = E.models.get(key);
      if (!entry) return;
      if (asSaved) entry.saved = text;
      if (entry.model) {
        if (entry.model.getValue() !== text) entry.model.pushEditOperations([], [{ range: entry.model.getFullModelRange(), text }], () => null);
      } else {
        entry.text = text;
        if (E.key === key) { E.fallback.ta.value = text; E.fallback.gutter(); }
      }
      emit("change", key, api.value(key) !== entry.saved);
    },
    markSaved(key) { const entry = E.models.get(key); if (entry) entry.saved = api.value(key); },
    dirty(key) { const entry = E.models.get(key); return !!entry && api.value(key) !== entry.saved; },
    focus() { if (E.editor) E.editor.focus(); else if (E.fallback) E.fallback.ta.focus(); },
    layout() { if (E.editor) E.editor.layout(); },
    reveal(line, col) {
      if (E.editor) {
        E.editor.setPosition({ lineNumber: line, column: col || 1 });
        E.editor.revealPositionInCenterIfOutsideViewport({ lineNumber: line, column: col || 1 });
        E.editor.focus();
      } else if (E.fallback) {
        const ta = E.fallback.ta, lines = ta.value.split("\n");
        let pos = 0;
        for (let i = 0; i < line - 1 && i < lines.length; i++) pos += lines[i].length + 1;
        ta.focus();
        ta.setSelectionRange(pos + (col || 1) - 1, pos + (col || 1) - 1);
        ta.scrollTop = Math.max(0, (line - 5) * 20);
      }
    },
    model(key) { const entry = E.models.get(key || E.key); return entry && entry.model; },
    // the URI a file's model has (also what go-to-definition hands back to open another file)
    uriOf(key) { return E.monaco.Uri.from({ scheme: "va", path: "/" + key.replace("\n", "/") }); },
    keyOfUri(uri) { const m = /^\/([^/]+)\/(.*)$/.exec(uri.path || ""); return uri.scheme === "va" && m ? m[1] + "\n" + m[2] : null; },
    keyOfModel(model) { for (const [k, e] of E.models) if (e.model === model) return k; return null; },
  };
  return api;
};
