"""Render Markdown from model replies into a Tk Text widget using tags.

Handles fenced code (with a Copy button), headings, lists, blockquotes,
tables, rules, links, bold / italic / strike and inline code. Text is only
ever inserted as text, so model output can't run anything.
"""
import re
import tkinter as tk
import webbrowser

INLINE = re.compile(
    r"(`[^`\n]+`)"                                # code
    r"|(\*\*[^*\n]+\*\*|__[^_\n]+__)"             # bold
    r"|(~~[^~\n]+~~)"                             # strike
    r"|(\[[^\]\n]+\]\(https?://[^\s)]+\))"        # [text](url)
    r"|(https?://[^\s<>()]+[^\s<>().,;:!?'\"])"   # bare url
    r"|((?<![\w*])\*[^*\s][^*\n]*\*(?!\*)|(?<![\w_])_[^_\s][^_\n]*_(?![\w_]))"  # italic
)
FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)")
LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def setup_tags(t, theme, fonts):
    body = fonts["body"]
    fam, size = body[0], body[1]
    t.tag_configure("p", spacing3=6)
    t.tag_configure("bold", font=(fonts.get("bold_family", fam), size, "bold"))
    t.tag_configure("italic", font=(fam, size, "italic"))
    t.tag_configure("strike", overstrike=True)
    t.tag_configure("code", font=fonts["mono"], background=theme["panel_hi"], foreground=theme["accent_2"])
    t.tag_configure("h1", font=fonts["md_h1"], spacing1=8, spacing3=6)
    t.tag_configure("h2", font=fonts["md_h2"], spacing1=8, spacing3=4)
    t.tag_configure("h3", font=fonts["md_h3"], spacing1=6, spacing3=4)
    t.tag_configure("codehead", font=fonts["small"], foreground=theme["faint"], background=theme["code_bg"],
                    lmargin1=10, lmargin2=10, spacing1=6)
    t.tag_configure("codeblock", font=fonts["mono"], background=theme["code_bg"], foreground=theme["text"],
                    lmargin1=12, lmargin2=12, spacing3=0)
    t.tag_configure("codeend", background=theme["code_bg"], font=(fam, 4), spacing3=8)
    t.tag_configure("quote", foreground=theme["dim"], lmargin1=16, lmargin2=16)
    t.tag_configure("li", lmargin1=8, lmargin2=24, spacing3=2)
    t.tag_configure("li2", lmargin1=26, lmargin2=42, spacing3=2)
    t.tag_configure("bullet", foreground=theme["accent"])
    t.tag_configure("link", foreground=theme["accent_2"], underline=True)
    t.tag_configure("hr", foreground=theme["line_2"], spacing1=4, spacing3=4)
    t.tag_configure("table", font=fonts["mono"], lmargin1=4, spacing3=0)
    t.tag_configure("think", foreground=theme["faint"], lmargin1=12, lmargin2=12, font=fonts["label"])
    t.tag_configure("thinkhead", foreground=theme["faint"], font=fonts["sub"], spacing3=4)
    t.tag_configure("caret", foreground=theme["accent"])
    t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
    t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor="xterm"))


class Renderer:
    def __init__(self, text, theme, fonts):
        self.t = text
        self.theme = theme
        self.fonts = fonts
        self.links = 0

    def ins(self, s, *tags):
        self.t.insert("end", s, tags)

    def inline(self, s, *base):
        pos = 0
        for m in INLINE.finditer(s):
            if m.start() > pos:
                self.ins(s[pos:m.start()], *base)
            code, bold, strike, link, url, ital = m.groups()
            if code:
                self.ins(code[1:-1], "code", *base)
            elif bold:
                self.ins(bold[2:-2], "bold", *base)
            elif strike:
                self.ins(strike[2:-2], "strike", *base)
            elif link:
                label, href = re.match(r"\[([^\]]+)\]\(([^)]+)\)", link).groups()
                self._link(label, href, base)
            elif url:
                self._link(url, url, base)
            elif ital:
                self.ins(ital[1:-1], "italic", *base)
            pos = m.end()
        if pos < len(s):
            self.ins(s[pos:], *base)

    def _link(self, label, href, base):
        self.links += 1
        tag = "link-%d" % self.links
        self.ins(label, "link", tag, *base)
        self.t.tag_bind(tag, "<Button-1>", lambda e, u=href: webbrowser.open(u))

    def code(self, lang, lines):
        t = self.t
        self.ins("  %s    " % (lang or "text").upper(), "codehead")
        btn = tk.Label(t, text="Copy", font=self.fonts["small"], bg=self.theme["code_bg"],
                       fg=self.theme["faint"], cursor="hand2")
        body = "\n".join(lines)

        def copy(_e=None):
            t.clipboard_clear()
            t.clipboard_append(body)
            btn.configure(text="Copied")
            btn.after(1200, lambda: btn.winfo_exists() and btn.configure(text="Copy"))

        btn.bind("<Button-1>", copy)
        t.window_create("end", window=btn)
        self.ins("\n", "codehead")
        self.ins(body + "\n", "codeblock")
        self.ins("\n", "codeend")

    def table(self, rows):
        cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
        head, body = cells[0], cells[2:]
        n = max(len(r) for r in [head] + body)
        widths = [max(len(r[i]) if i < len(r) else 0 for r in [head] + body) for i in range(n)]
        fmt = lambda r: "  ".join((r[i] if i < len(r) else "").ljust(widths[i]) for i in range(n)).rstrip()
        self.ins(fmt(head) + "\n", "table", "bold")
        self.ins("  ".join("─" * w for w in widths) + "\n", "table", "hr")
        for r in body:
            self.ins(fmt(r) + "\n", "table")
        self.ins("\n", "p")

    def render(self, src, base=()):
        lines = src.replace("\r\n", "\n").split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            f = FENCE.match(line)
            if f:
                buf = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith(f.group(1)):
                    buf.append(lines[i])
                    i += 1
                i += 1
                self.code(f.group(2), buf)
                continue
            if not line.strip():
                i += 1
                continue
            h = re.match(r"^(#{1,6})\s+(.*)$", line)
            if h:
                self.inline(h.group(2), "h%d" % min(len(h.group(1)), 3), *base)
                self.ins("\n", *base)
                i += 1
                continue
            if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
                self.ins("─" * 40 + "\n", "hr")
                i += 1
                continue
            if TABLE_ROW.match(line) and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]):
                rows = []
                while i < len(lines) and TABLE_ROW.match(lines[i]):
                    rows.append(lines[i])
                    i += 1
                if len(rows) >= 2:
                    self.table(rows)
                else:  # separator still streaming in
                    self.inline(rows[0], "p", *base)
                    self.ins("\n", *base)
                continue
            if line.lstrip().startswith(">"):
                buf = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                self.render("\n".join(buf), base=tuple(base) + ("quote",))
                continue
            m = LIST.match(line)
            if m:
                while i < len(lines):
                    m = LIST.match(lines[i])
                    if not m:
                        if lines[i].strip() and lines[i].startswith("  "):
                            self.inline(" " + lines[i].strip(), *base)
                            i += 1
                            continue
                        break
                    tag = "li2" if len(m.group(1)) >= 2 else "li"
                    marker = "•" if not m.group(2)[0].isdigit() else m.group(2)
                    self.ins("\n" if self.t.get("end-2c") not in ("\n", "") else "", *base)
                    self.ins(marker + "  ", "bullet", tag, *base)
                    self.inline(m.group(3), tag, *base)
                    i += 1
                self.ins("\n", "p", *base)
                continue
            buf = []
            while i < len(lines) and lines[i].strip() and not re.match(
                    r"^(#{1,6}\s|\s*(```|~~~)|\s*>|\s*([-*+]|\d+[.)])\s)", lines[i]) and not TABLE_ROW.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if not buf:
                buf.append(lines[i])
                i += 1
            self.inline("\n".join(buf), "p", *base)
            self.ins("\n", "p", *base)


def render(text, src, theme, fonts):
    Renderer(text, theme, fonts).render(src)
