// Markdown → HTML for model replies. Every piece of text is escaped first, so
// model output can't inject markup or scripts; links are limited to http(s).
(function () {
  "use strict";

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function inline(text) {
    // pull code spans out first so their contents aren't formatted
    const codes = [];
    let s = text.replace(/`([^`\n]+)`/g, (_, c) => "\u0000" + (codes.push(c) - 1) + "\u0000");
    s = esc(s);
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, t, u) => '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + "</a>");
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g,
      (_, pre, u) => pre + '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + "</a>");
    s = s.replace(/\*\*([^*\n]+)\*\*|__([^_\n]+)__/g, (_, a, b) => "<strong>" + (a || b) + "</strong>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    s = s.replace(/(^|[^\w*])\*([^*\s][^*\n]*?)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^\w_])_([^_\s][^_\n]*?)_(?![\w_])/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<code>" + esc(codes[+i]) + "</code>");
  }

  function table(rows) {
    const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
    const head = cells(rows[0]);
    let h = "<table><thead><tr>" + head.map(c => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
    for (const r of rows.slice(2)) h += "<tr>" + cells(r).map(c => "<td>" + inline(c) + "</td>").join("") + "</tr>";
    return h + "</tbody></table>";
  }

  function render(src) {
    const lines = (src || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0, para = [];
    const flush = () => { if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; } };

    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(```|~~~)\s*([\w+#.-]*)/);
      if (fence) {
        flush();
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        i++;
        out.push('<pre><div class="code-head"><span>' + esc(fence[2] || "code") +
          '</span><button type="button" data-copy>Copy</button></div><code>' + esc(body.join("\n")) + "</code></pre>");
        continue;
      }
      if (!line.trim()) { flush(); i++; continue; }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flush(); const n = Math.min(h[1].length, 4); out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">"); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push("<hr>"); i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        flush();
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        out.push(table(rows));
        continue;
      }
      if (/^\s*>/.test(line)) {
        flush();
        const q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote>" + render(q.join("\n")) + "</blockquote>");
        continue;
      }
      const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (li) {
        flush();
        // a run of list items; deeper indentation nests one level
        const ordered = /\d/.test(li[2]);
        let html = ordered ? "<ol>" : "<ul>", depth = 0;
        while (i < lines.length) {
          const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
          if (!m) {
            if (lines[i].trim() && /^\s{2,}/.test(lines[i])) { html += "<br>" + inline(lines[i].trim()); i++; continue; }
            break;
          }
          const d = m[1].replace(/\t/g, "    ").length >= 2 ? 1 : 0;
          if (d > depth) { html += "<ul>"; depth = 1; } else if (d < depth) { html += "</ul>"; depth = 0; }
          html += "<li>" + inline(m[3]) + "</li>";
          i++;
        }
        if (depth) html += "</ul>";
        out.push(html + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      para.push(line);
      i++;
    }
    flush();
    return out.join("");
  }

  // models that think inline: "<think>…</think>answer"
  function splitThink(text) {
    const t = (text || "").replace(/^\s+/, "");
    if (!t.startsWith("<think>")) return ["", text || "", false];
    const end = t.indexOf("</think>");
    if (end < 0) return [t.slice(7), "", true];
    return [t.slice(7, end), t.slice(end + 8).replace(/^\s+/, ""), false];
  }

  window.MD = { render, esc, splitThink };
})();
