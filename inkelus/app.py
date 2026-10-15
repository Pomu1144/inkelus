"""Inkelus — a desktop chat app for local LLMs, with a built-in sandbox browser.

Pure Python: tkinter + the standard library. Network calls run on worker
threads and report back to the Tk thread through a queue.
"""
import base64
import json
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, ttk

from . import mdtk, net, providers, theme as themes, web
from .store import Store, new_id
from .widgets import (Btn, NavItem, Panel, ScrollFrame, Slip, Tab, bind_wheel, entry, style_ttk, textbox)

ASSETS = themes.ASSETS
TOOL_RESULT_LIMIT = 15000


# ── Formatting helpers ─────────────────────────────────────────────────────
def fmt_bytes(n):
    if n is None:
        return ""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.1f %s" if n < 10 and u != "B" else "%.0f %s") % (n, u)
        n /= 1024


def parse_time(iso):
    if not iso:
        return None
    try:
        iso = iso.replace("Z", "+00:00")
        if "." in iso:  # trim nanoseconds that fromisoformat can't read
            head, rest = iso.split(".", 1)
            frac = "".join(ch for ch in rest if ch.isdigit())[:6]
            tz = rest[len("".join(ch for ch in rest if ch.isdigit())):]
            iso = head + "." + frac + tz
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def ago(iso):
    t = parse_time(iso)
    if t is None:
        return ""
    s = time.time() - t
    if s < 60:
        return "just now"
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


def until(iso):
    t = parse_time(iso)
    if t is None:
        return ""
    s = t - time.time()
    if s > 1e8:
        return "kept loaded"
    if s <= 0:
        return "unloading"
    return "unloads in %ds" % s if s < 60 else "unloads in %dm" % (s // 60)


def stats_text(st):
    if not st:
        return ""
    parts = []
    if st.get("tokens"):
        parts.append("%s%d tokens" % ("~" if st.get("approx") else "", st["tokens"]))
    if st.get("tps"):
        parts.append("%.1f tok/s" % st["tps"])
    if st.get("seconds"):
        parts.append("%.1fs" % st["seconds"])
    return " · ".join(parts)


def split_think(text):
    t = text.lstrip()
    if not t.startswith("<think>"):
        return "", text, False
    end = t.find("</think>")
    if end < 0:
        return t[7:].strip(), "", True
    return t[7:end].strip(), t[end + 8:].lstrip(), False


def fit_text(font, text, px):
    """Shorten text with an ellipsis so it fits in px pixels of the given tkfont.Font."""
    if px <= 0 or font.measure(text) <= px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid].rstrip() + "…") <= px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def split_model(v):
    if not v or "::" not in v:
        return None, None
    a, b = v.split("::", 1)
    return a, b


# ── Message view ───────────────────────────────────────────────────────────
class MessageView(tk.Frame):
    def __init__(self, app, master, msg, idx, live=False):
        th, f = app.th, app.f
        super().__init__(master, bg=th["bg"])
        self.app, self.msg, self.idx = app, msg, idx
        role = msg["role"]
        head = tk.Frame(self, bg=th["bg"])
        head.pack(fill="x", pady=(0, 4))
        if role == "tool":
            self._tool(head)
            return
        who = "YOU" if role == "user" else (split_model(msg.get("model"))[1] or "MODEL").upper()
        dot = tk.Canvas(head, width=10, height=10, bg=th["bg"], highlightthickness=0)
        dot.create_polygon(5, 1, 9, 5, 5, 9, 1, 5, fill=th["sel"] if role == "user" else th["accent"])
        dot.pack(side="left", padx=(0, 6))
        tk.Label(head, text=" ".join(who) if th["name"] == "ink" else who, font=f["sub"], bg=th["bg"],
                 fg=th["dim"]).pack(side="left")
        if not live:
            acts = [("Copy", "copy")]
            if role == "user":
                acts.append(("Edit", "edit"))
            if role == "assistant":
                acts.append(("Retry", "regen"))
            acts.append(("Delete", "delete"))
            for label, act in reversed(acts):
                b = tk.Label(head, text=label.upper(), font=f["sub"], bg=th["bg"], fg=th["faint"], cursor="hand2",
                             padx=6)
                b.pack(side="right")
                b.bind("<Button-1>", lambda e, a=act: app.message_action(a, idx))
                b.bind("<Enter>", lambda e, w=b: w.configure(fg=th["text"]))
                b.bind("<Leave>", lambda e, w=b: w.configure(fg=th["faint"]))

        if role == "assistant" and msg.get("tool_calls") and not (msg.get("content") or "").strip() \
                and not msg.get("thinking"):
            tk.Label(self, text="→ using web tools", font=f["label"], bg=th["bg"], fg=th["faint"]).pack(anchor="w")
            return

        is_user = role == "user"
        err = msg.get("error")
        bubble = tk.Frame(self, bg=th["panel_hi"] if is_user else th["panel"], highlightthickness=1,
                          highlightbackground=th["line"])
        bubble.pack(fill="x")
        edge = tk.Frame(bubble, bg=th["red"] if err else (th["sel"] if is_user else th["panel"]), width=2)
        edge.pack(side="left", fill="y")
        if not is_user and not err:
            tk.Frame(bubble, bg=th["accent"], height=2, width=24).place(x=14, y=0)
        self.text = tk.Text(bubble, bg=bubble["bg"], fg=th["red"] if err else th["text"], font=f["body"],
                            relief="flat", wrap="word", padx=16, pady=12, height=1, width=10, bd=0,
                            highlightthickness=0, cursor="xterm", spacing1=1, spacing3=1,
                            selectbackground=th["accent"], selectforeground=th["on_accent"],
                            insertwidth=0)
        self.text.pack(side="left", fill="x", expand=True)
        mdtk.setup_tags(self.text, th, f)
        self.fill(live)
        self.text.bind("<Configure>", lambda e: self.fit())
        bind_wheel(self.text, app.scroll_canvas)
        if msg.get("images"):
            tk.Label(self, text="📎 %d image%s attached" % (len(msg["images"]), "" if len(msg["images"]) == 1 else "s"),
                     font=f["small"], bg=th["bg"], fg=th["faint"]).pack(anchor="w", pady=(4, 0))
        if msg.get("stats"):
            tk.Label(self, text=stats_text(msg["stats"]), font=f["small"], bg=th["bg"], fg=th["faint"]).pack(
                anchor="w", pady=(4, 0))

    def _tool(self, head):
        th, f, m = self.app.th, self.app.f, self.msg
        box = tk.Frame(self, bg=th["panel_2"], highlightthickness=1,
                       highlightbackground=th["red"] if m.get("is_error") else th["line"])
        box.pack(fill="x", padx=(24, 0))
        args = json.dumps(m.get("arguments") or {}, ensure_ascii=False)
        if len(args) > 140:
            args = args[:137] + "…"
        top = tk.Frame(box, bg=th["panel_2"])
        top.pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(top, text="⚙  " + m.get("name", "tool"), font=(f["mono"][0], f["mono"][1], "bold"),
                 bg=th["panel_2"], fg=th["accent_2"]).pack(side="left")
        tk.Label(top, text="  " + args, font=f["mono"], bg=th["panel_2"], fg=th["faint"]).pack(side="left")
        status = m.get("status") or ("error" if m.get("is_error") else "done")
        tk.Label(top, text=status.upper(), font=f["sub"], bg=th["panel_2"],
                 fg=th["red"] if status in ("error", "denied") else th["faint"]).pack(side="right")
        result = (m.get("content") or "").strip()
        if result:
            short = result if len(result) < 240 else result[:240].rstrip() + "…"
            lab = tk.Label(box, text=short, font=f["small"], bg=th["panel_2"], fg=th["dim"], justify="left",
                           anchor="w", wraplength=700, cursor="hand2")
            lab.pack(fill="x", padx=12, pady=(0, 8))
            lab.bind("<Configure>", lambda e: lab.configure(wraplength=max(e.width - 10, 100)))

            def toggle(_e, state={"open": False}):
                state["open"] = not state["open"]
                full = result if len(result) < 6000 else result[:6000] + "\n…"
                lab.configure(text=full if state["open"] else short)

            lab.bind("<Button-1>", toggle)
        else:
            tk.Frame(box, bg=th["panel_2"], height=6).pack()

    def fill(self, live=False):
        t, m, th = self.text, self.msg, self.app.th
        t.configure(state="normal")
        t.delete("1.0", "end")
        if m.get("error"):
            t.insert("end", m["error"])
        elif m["role"] == "user":
            t.insert("end", m.get("content", ""))
        else:
            think, body, open_ = split_think(m.get("content", ""))
            think = "\n".join(x for x in (m.get("thinking", "").strip(), think) if x)
            if think:
                t.insert("end", "R E A S O N I N G\n", "thinkhead")
                shown = think if (live and (open_ or not body.strip())) or len(think) < 600 else think[:600] + "…"
                t.insert("end", shown + "\n\n", "think")
            mdtk.render(t, body, th, self.app.f)
            if live:
                t.insert("end", "▌", "caret")
        while t.get("end-2c") == "\n" and t.index("end-2c") != "1.0":
            t.delete("end-2c")
        t.configure(state="disabled")
        self.fit()

    def fit(self):
        t = self.text
        try:
            n = t.count("1.0", "end-1c", "displaylines")
            n = (n[0] if isinstance(n, tuple) else n or 0) + 1
        except tk.TclError:
            return
        if n and int(t["height"]) != n:
            t.configure(height=n)
            self.app.queue_scrollregion()


# ── The application ────────────────────────────────────────────────────────
class App:
    def __init__(self):
        themes.register_fonts()
        self.root = tk.Tk()
        self.root.title("Inkelus")
        self.root.geometry("1280x820")
        self.root.minsize(860, 600)
        self.store = Store()
        self.q = queue.Queue()
        self.models = {}          # conn id -> [model dict] or None
        self.active_id = self.store.chats[0]["id"] if self.store.chats else None
        self.stream = None        # {"chat_id", "cancel": Event, "views": ...}
        self.pending_images = []  # [(name, b64)]
        self.sandboxes = {}       # chat id -> web.Sandbox (isolated per chat)
        self.pull_cancel = None
        self._last_paint = 0
        try:
            self.icon = tk.PhotoImage(file=os.path.join(ASSETS, "enso_small.png"))
            self.root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        self.build()
        self.root.after(40, self.poll)
        self.refresh_models()
        self.check_status()

    # ── Build / rebuild (style switch) ──────────────────────────────────
    def build(self):
        for w in self.root.winfo_children():
            w.destroy()
        style = self.store.settings.get("style", "ink")
        self.th = themes.STYLES.get(style, themes.INK)
        self.f = themes.fonts_for(style, set(tkfont.families(self.root)))
        self.label_font = tkfont.Font(root=self.root, font=self.f["label"])
        self.title_font = tkfont.Font(root=self.root, font=self.f["title"])
        th = self.th
        self.root.configure(bg=th["bg"])
        style_ttk(self.root, th, self.f)
        self.bg_img = tk.PhotoImage(file=os.path.join(ASSETS, "bg_ink.png"))
        self.enso = tk.PhotoImage(file=os.path.join(ASSETS, "enso.png"))
        self.enso_small = tk.PhotoImage(file=os.path.join(ASSETS, "enso_small.png"))

        self.rail = tk.Frame(self.root, bg=th["rail"], width=250)
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)
        tk.Frame(self.root, bg=th["line"], width=1).pack(side="left", fill="y")
        self.main = tk.Frame(self.root, bg=th["bg"])
        self.main.pack(side="left", fill="both", expand=True)
        self.build_rail()
        self.views = {"chat": self.build_chat(), "models": self.build_models(), "settings": self.build_settings()}
        self.show_view("chat")

    def title_text(self, s):
        return s if self.th["name"] == "ink" else s.upper()

    def page_head(self, parent, title, sub):
        th, f = self.th, self.f
        head = tk.Frame(parent, bg=th["bg"])
        head.pack(fill="x", padx=28, pady=(18, 12))
        tools = tk.Frame(head, bg=th["bg"])
        tools.pack(side="right")
        left = tk.Frame(head, bg=th["bg"])
        left.pack(side="left", fill="x", expand=True)
        if th["name"] == "glass":
            tk.Frame(left, bg=th["accent"], width=3).pack(side="left", fill="y", padx=(0, 10))
        box = tk.Frame(left, bg=th["bg"])
        box.pack(side="left", fill="x", expand=True)
        lab = tk.Label(box, text=self.spaced(title), font=f["title"], bg=th["bg"], fg=th["text"], anchor="w", width=1)
        lab.pack(anchor="w", fill="x")
        tk.Label(box, text=sub.upper(), font=f["sub"], bg=th["bg"], fg=th["dim"], anchor="w", width=1).pack(
            anchor="w", fill="x")
        state = {"stacked": None}

        def relayout(e):
            # narrow window: the tools drop below the title instead of squeezing it
            stacked = e.width < tools.winfo_reqwidth() + 260
            if stacked != state["stacked"]:
                state["stacked"] = stacked
                tools.pack_forget()
                left.pack_forget()
                if stacked:
                    left.pack(side="top", fill="x")
                    tools.pack(side="top", anchor="w", pady=(10, 0))
                else:
                    tools.pack(side="right")
                    left.pack(side="left", fill="x", expand=True)

        head.bind("<Configure>", relayout)
        return lab, tools

    def spaced(self, s):
        """Ink titles use wide letter-spacing; Tk can't track, so add hair spaces."""
        return " ".join(s) if self.th["name"] == "ink" else s.upper()

    # ── Rail ────────────────────────────────────────────────────────────
    def build_rail(self):
        th, f, r = self.th, self.f, self.rail
        brand = tk.Frame(r, bg=th["rail"])
        brand.pack(fill="x", padx=18, pady=(18, 14))
        tk.Label(brand, image=self.enso_small, bg=th["rail"]).pack(side="left")
        tk.Label(brand, text="I N K E L U S" if th["name"] == "ink" else "INKELUS", font=f["brand"],
                 bg=th["rail"], fg=th["dim"], justify="left").pack(side="left", padx=10)
        self.new_btn = Slip(r, th, f, "New", self.new_chat, sub="新しい会話", width=214, height=50)
        self.new_btn.pack(padx=18, pady=(0, 10))
        self.nav = {}
        for key, label in (("chat", "Chat"), ("models", "Models"), ("settings", "Settings")):
            n = NavItem(r, th, f, key, label, self.show_view)
            n.pack(fill="x")
            self.nav[key] = n
        tk.Frame(r, bg=th["line"], height=1).pack(fill="x", pady=(10, 0))
        head = tk.Frame(r, bg=th["rail"])
        head.pack(fill="x", padx=(18, 12), pady=(10, 6))
        tk.Label(head, text="S C R O L L S" if th["name"] == "ink" else "CHATS", font=f["sub"], bg=th["rail"],
                 fg=th["faint"]).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.render_convos())
        e = entry(head, th, f, width=14, textvariable=self.search_var)
        e.configure(font=f["label"])
        e.pack(side="right", fill="x", expand=True, padx=(10, 0), ipady=3)

        foot = tk.Frame(r, bg=th["rail"])
        foot.pack(side="bottom", fill="x")
        tk.Frame(foot, bg=th["line"], height=1).pack(fill="x")
        row = tk.Frame(foot, bg=th["rail"])
        row.pack(fill="x", padx=18, pady=10)
        self.status_dot = tk.Canvas(row, width=10, height=10, bg=th["rail"], highlightthickness=0)
        self.status_dot.pack(side="left")
        self.status_lab = tk.Label(row, text="Connecting…", font=f["small"], bg=th["rail"], fg=th["dim"], anchor="w")
        self.status_lab.pack(side="left", padx=8, fill="x")
        self.set_status(*getattr(self, "_status", (None, "Connecting…")))

        self.convo_box = ScrollFrame(r, th, bg=th["rail"])
        self.convo_box.pack(fill="both", expand=True)
        self.render_convos()

    def set_status(self, ok, text):
        self._status = (ok, text)
        th = self.th
        self.status_dot.delete("all")
        col = th["faint"] if ok is None else (th["green"] if ok else th["red"])
        self.status_dot.create_oval(1, 1, 9, 9, fill=col, outline="")
        self.status_lab.configure(text=text)

    def render_convos(self):
        th, f = self.th, self.f
        inner = self.convo_box.inner
        for w in inner.winfo_children():
            w.destroy()
        q = self.search_var.get().strip().lower()
        chats = sorted(self.store.chats, key=lambda c: -c["updated"])
        if q:
            chats = [c for c in chats if q in c["title"].lower()
                     or any(q in (m.get("content") or "").lower() for m in c["messages"])]
        if not chats:
            tk.Label(inner, text="No matches." if q else "No conversations yet.", font=f["label"], bg=th["rail"],
                     fg=th["faint"]).pack(anchor="w", padx=20, pady=8)
            return
        for c in chats:
            active = c["id"] == self.active_id
            bg = th["wash"] if active and th["name"] == "ink" else (th["panel_hi"] if active else th["rail"])
            row = tk.Frame(inner, bg=bg, cursor="hand2")
            row.pack(fill="x")
            tk.Frame(row, bg=th["accent"] if active else bg, width=2).pack(side="left", fill="y")
            x = tk.Label(row, text="✕", font=f["small"], bg=bg, fg=bg, padx=10, cursor="hand2")
            x.pack(side="right")
            title = fit_text(self.label_font, c["title"], 250 - 2 - 40 - 32 - 16)
            lab = tk.Label(row, text=title, font=f["label"], bg=bg, fg=th["text"] if active else th["dim"],
                           anchor="w", padx=20, pady=8, width=1)
            lab.pack(side="left", fill="x", expand=True)
            for w in (row, lab):
                w.bind("<Button-1>", lambda e, cid=c["id"]: self.open_chat(cid))
                w.bind("<Button-3>", lambda e, cid=c["id"]: self.convo_menu(e, cid))
                w.bind("<Enter>", lambda e, xx=x: xx.configure(fg=th["faint"]))
                w.bind("<Leave>", lambda e, xx=x, b=bg: xx.configure(fg=b))
            x.bind("<Button-1>", lambda e, cid=c["id"]: self.delete_chat(cid))
            x.bind("<Enter>", lambda e, xx=x: xx.configure(fg=th["red"]))
            bind_wheel(row, self.convo_box.canvas)
            bind_wheel(lab, self.convo_box.canvas)

    def convo_menu(self, e, cid):
        th = self.th
        m = tk.Menu(self.root, tearoff=0, bg=th["panel_2"], fg=th["text"], activebackground=th["accent"],
                    activeforeground=th["on_accent"], font=self.f["label"])
        m.add_command(label="Open", command=lambda: self.open_chat(cid))
        m.add_command(label="Export as Markdown…", command=lambda: self.export_chat(self.chat_by_id(cid)))
        m.add_separator()
        m.add_command(label="Delete", command=lambda: self.delete_chat(cid))
        m.tk_popup(e.x_root, e.y_root)

    def show_view(self, name):
        for k, v in self.views.items():
            if k == name:
                v.pack(fill="both", expand=True)
            else:
                v.pack_forget()
        for k, n in self.nav.items():
            n.set_active(k == name)
        self.view = name
        if name == "models":
            self.load_models_view()
        if name == "settings":
            self.fill_settings()

    # ── Chat view ───────────────────────────────────────────────────────
    def build_chat(self):
        th, f = self.th, self.f
        v = tk.Frame(self.main, bg=th["bg"])
        self.title_lab, tools = self.page_head(v, "New conversation", "Conversation · 対話")
        self.chat_head = tools.master
        self.title_lab.configure(cursor="xterm")
        self.title_lab.bind("<Button-1>", lambda e: self.rename_chat())

        pick = tk.Frame(tools, bg=th["panel_2"], highlightthickness=1, highlightbackground=th["line_2"])
        pick.pack(side="left", padx=(0, 8))
        tk.Label(pick, text="MODEL", font=f["sub"], bg=th["panel_2"], fg=th["faint"]).pack(side="left", padx=(12, 4))
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(pick, textvariable=self.model_var, state="readonly", width=24, font=f["body"])
        self.model_box.pack(side="left", padx=(0, 4), pady=2)
        self.model_box.bind("<<ComboboxSelected>>", self.on_model_pick)
        self.model_values = []

        self.browser_chip = tk.Label(tools, font=f["label"], padx=12, pady=6, cursor="hand2", highlightthickness=1)
        self.browser_chip.pack(side="left", padx=(0, 8))
        self.browser_chip.bind("<Button-1>", lambda e: self.toggle_browser())
        self.params_btn = Btn(tools, th, f, "Parameters", self.toggle_params, small=True)
        self.params_btn.pack(side="left", padx=(0, 8), fill="y")
        Btn(tools, th, f, "Export", lambda: self.export_chat(self.active()), small=True).pack(side="left", fill="y")
        self.title_lab.bind("<Configure>", lambda e: self.render_title(), add="+")
        char_px = tkfont.Font(root=self.root, font=f["body"]).measure("0")

        def fit_picker(e):
            # on narrow windows the model picker gives up width so every button stays whole
            others = tools.winfo_reqwidth() - self.model_box.winfo_reqwidth()
            chars = max(10, min(24, (e.width - others - 40) // max(char_px, 1)))
            if int(self.model_box.cget("width")) != chars:
                self.model_box.configure(width=chars)

        self.chat_head.bind("<Configure>", fit_picker, add="+")

        # parameters drawer
        self.params = Panel(v, th)
        pb = self.params.body
        tk.Label(pb, text="System prompt", font=f["label"], bg=th["panel"], fg=th["dim"]).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.p_system = textbox(pb, th, f, height=2)
        self.p_system.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        self.p_vars = {}
        for col, (key, label, lo, hi) in enumerate((("temperature", "Temperature", 0, 2), ("top_p", "Top P", 0, 1))):
            box = tk.Frame(pb, bg=th["panel"])
            box.grid(row=2, column=col, sticky="ew", padx=(0, 16 if col == 0 else 0))
            var = tk.DoubleVar()
            lab = tk.Label(box, font=f["label"], bg=th["panel"], fg=th["dim"])
            lab.pack(anchor="w")
            ttk.Scale(box, from_=lo, to=hi, variable=var, style="Ink.Horizontal.TScale",
                      command=lambda val, k=key, l=label, lb=lab: self.on_param(k, l, lb)).pack(fill="x", pady=4)
            self.p_vars[key] = (var, lab, label)
        for col, (key, label) in enumerate((("num_ctx", "Context (num_ctx)"), ("max_tokens", "Max tokens"))):
            box = tk.Frame(pb, bg=th["panel"])
            box.grid(row=3, column=col, sticky="ew", padx=(0, 16 if col == 0 else 0), pady=(6, 0))
            tk.Label(box, text=label, font=f["label"], bg=th["panel"], fg=th["dim"]).pack(anchor="w")
            var = tk.StringVar()
            e = entry(box, th, f, width=12, textvariable=var)
            e.pack(fill="x", pady=4, ipady=3)
            var.trace_add("write", lambda *a: self.save_params())
            self.p_vars[key] = (var, None, label)
        for c in range(2):
            pb.grid_columnconfigure(c, weight=1, uniform="p")
        self.p_system.bind("<KeyRelease>", lambda e: self.save_params())

        # composer (packed before transcript so it keeps its height)
        comp = tk.Frame(v, bg=th["panel"], highlightthickness=1, highlightbackground=th["line"])
        comp.pack(side="bottom", fill="x", padx=28, pady=(8, 18))
        tk.Frame(comp, bg=th["accent"], height=2, width=24).place(x=14, y=0)
        self.attach_row = tk.Frame(comp, bg=th["panel"])
        self.attach_row.pack(fill="x", padx=14)
        self.prompt = tk.Text(comp, bg=th["panel"], fg=th["text"], insertbackground=th["accent"], relief="flat",
                              font=f["body"], height=2, wrap="word", padx=16, pady=12, highlightthickness=0, undo=True)
        self.prompt.pack(fill="x")
        self.placeholder = True
        self._set_placeholder()
        self.prompt.bind("<FocusIn>", lambda e: self._clear_placeholder())
        self.prompt.bind("<FocusOut>", lambda e: self._set_placeholder())
        self.prompt.bind("<Return>", self.on_enter)
        self.prompt.bind("<KeyRelease>", lambda e: self.autosize())
        bar = tk.Frame(comp, bg=th["panel"])
        bar.pack(fill="x", padx=12, pady=(0, 10))
        Btn(bar, th, f, "Attach image", self.attach, small=True).pack(side="left")
        self.stats_lab = tk.Label(bar, text="", font=f["small"], bg=th["panel"], fg=th["faint"])
        self.stats_lab.pack(side="left", padx=12)
        self.send_btn = Slip(bar, th, f, "Send", self.send, width=160, height=40)
        self.send_btn.pack(side="right")

        # transcript: a canvas with the ink backdrop and a frame of messages
        wrap = self.transcript_wrap = tk.Frame(v, bg=th["bg"])
        wrap.pack(fill="both", expand=True)
        self.scroll_canvas = c = tk.Canvas(wrap, bg=th["bg"], highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=c.yview, style="Ink.Vertical.TScrollbar")
        c.configure(yscrollcommand=lambda a, b: (sb.set(a, b), self.place_backdrop()))
        sb.pack(side="right", fill="y")
        c.pack(side="left", fill="both", expand=True)
        self.bg_item = c.create_image(0, 0, image=self.bg_img, anchor="ne")
        self.msg_frame = tk.Frame(c, bg=th["bg"])
        self.msg_win = c.create_window(0, 0, window=self.msg_frame, anchor="n")
        self.msg_frame.bind("<Configure>", lambda e: self.queue_scrollregion())
        c.bind("<Configure>", self.on_canvas_resize)
        bind_wheel(c, c)
        bind_wheel(self.msg_frame, c)
        self.render_chat()
        return v

    def on_canvas_resize(self, e):
        w = min(e.width - 56, 900)
        self.scroll_canvas.coords(self.msg_win, e.width / 2, 8)
        self.scroll_canvas.itemconfigure(self.msg_win, width=max(w, 300))
        self.queue_scrollregion()
        if getattr(self, "empty_items", None):
            self.draw_empty()

    def queue_scrollregion(self):
        if not getattr(self, "_sr_pending", False):
            self._sr_pending = True
            self.root.after_idle(self.update_scrollregion)

    def update_scrollregion(self):
        self._sr_pending = False
        c = self.scroll_canvas
        if not c.winfo_exists():
            return
        self.msg_frame.update_idletasks()
        h = max(self.msg_frame.winfo_reqheight() + 24, c.winfo_height())
        c.configure(scrollregion=(0, 0, c.winfo_width(), h))
        self.place_backdrop()

    def place_backdrop(self):
        c = self.scroll_canvas
        c.coords(self.bg_item, c.winfo_width(), c.canvasy(0))
        c.tag_lower(self.bg_item)

    def _set_placeholder(self):
        if not self.prompt.get("1.0", "end").strip():
            self.placeholder = True
            self.prompt.insert("1.0", "Write to the model…   (Enter to send, Shift+Enter for a new line)")
            self.prompt.configure(fg=self.th["faint"])

    def _clear_placeholder(self):
        if self.placeholder:
            self.prompt.delete("1.0", "end")
            self.prompt.configure(fg=self.th["text"])
            self.placeholder = False

    def prompt_text(self):
        return "" if self.placeholder else self.prompt.get("1.0", "end").strip()

    def autosize(self):
        try:
            n = self.prompt.count("1.0", "end", "displaylines")
            n = n[0] if isinstance(n, tuple) else n
        except tk.TclError:
            return
        self.prompt.configure(height=max(2, min(n or 2, 10)))

    def on_enter(self, e):
        if e.state & 0x0001:  # Shift: newline
            return None
        self.send()
        return "break"

    def active(self):
        return self.chat_by_id(self.active_id)

    def chat_by_id(self, cid):
        return next((c for c in self.store.chats if c["id"] == cid), None)

    def new_chat(self, model=None):
        c = self.store.new_chat(model or self.current_model())
        self.active_id = c["id"]
        self.store.save_chats()
        self.render_convos()
        self.render_chat()
        self.show_view("chat")
        self.prompt.focus_set()
        return c

    def open_chat(self, cid):
        self.active_id = cid
        self.render_convos()
        self.render_chat()
        self.show_view("chat")

    def delete_chat(self, cid):
        c = self.chat_by_id(cid)
        if not c or not messagebox.askyesno("Delete conversation", 'Delete "%s"?' % c["title"], parent=self.root):
            return
        if self.stream and self.stream["chat_id"] == cid:
            self.stream["cancel"].set()
        self.store.chats.remove(c)
        if self.active_id == cid:
            self.active_id = self.store.chats[0]["id"] if self.store.chats else None
        self.store.save_chats()
        self.render_convos()
        self.render_chat()

    def rename_chat(self):
        c = self.active()
        if not c:
            return
        th, f = self.th, self.f
        lab = self.title_lab
        var = tk.StringVar(value=c["title"])
        e = tk.Entry(lab.master, textvariable=var, font=f["title"], bg=th["bg"], fg=th["text"], relief="flat",
                     insertbackground=th["accent"], highlightthickness=0)
        e.pack(anchor="w", fill="x", before=lab)
        lab.pack_forget()
        e.focus_set()
        e.select_range(0, "end")
        state = {"done": False}

        def done(_e=None):
            if state["done"]:
                return
            state["done"] = True
            t = var.get().strip()
            if t and t != c["title"]:
                c["title"], c["titled"] = t, True
                self.store.save_chats()
                self.render_convos()
            lab.pack(anchor="w", fill="x", before=e)
            e.destroy()
            self.render_title()

        e.bind("<Return>", done)
        e.bind("<FocusOut>", done)
        e.bind("<Escape>", lambda ev: (var.set(c["title"]), done()))

    def render_title(self):
        c = self.active()
        text = self.spaced(c["title"] if c else "New conversation")
        w = self.title_lab.winfo_width()
        if w > 20:
            text = fit_text(self.title_font, text, w - 4)
        if self.title_lab.cget("text") != text:
            self.title_lab.configure(text=text)

    def render_browser_chip(self):
        th = self.th
        c = self.active()
        on = bool(c and c.get("browser"))
        text = "Web · on" if on else "Web · off"
        if th["name"] == "glass":
            text = text.upper()
        if on:
            self.browser_chip.configure(text=text, bg=th["wash"] if th["name"] == "ink" else th["sel"],
                                        fg=th["accent_2"] if th["name"] == "ink" else th["on_sel"],
                                        highlightbackground=th["accent"])
        else:
            self.browser_chip.configure(text=text, bg=th["panel_2"], fg=th["dim"], highlightbackground=th["line_2"])

    def toggle_browser(self):
        c = self.active() or self.new_chat()
        c["browser"] = not c.get("browser")
        self.store.save_chats()
        self.render_browser_chip()
        self.toast("Web sandbox on: the model can search and read pages (read-only)." if c["browser"]
                   else "Web sandbox off for this chat.")

    def toggle_params(self):
        if self.params.winfo_ismapped():
            self.params.pack_forget()
        else:
            self.params.pack(fill="x", padx=28, pady=(0, 10), before=self.transcript_wrap)

    def fill_params(self):
        c = self.active()
        p = c["params"] if c else {"system": self.store.settings["defaults"]["system"], "temperature": 0.7,
                                   "top_p": 0.9}
        self._filling = True
        self.p_system.delete("1.0", "end")
        self.p_system.insert("1.0", p.get("system") or "")
        for key in ("temperature", "top_p"):
            var, lab, label = self.p_vars[key]
            var.set(p.get(key) if p.get(key) is not None else (0.7 if key == "temperature" else 0.9))
            lab.configure(text="%s   %.2f" % (label, var.get()))
        for key in ("num_ctx", "max_tokens"):
            self.p_vars[key][0].set(str(p.get(key) or ""))
        self._filling = False

    def on_param(self, key, label, lab):
        lab.configure(text="%s   %.2f" % (label, self.p_vars[key][0].get()))
        self.save_params()

    def save_params(self):
        if getattr(self, "_filling", False):
            return
        c = self.active()
        if not c:
            return

        def num(v):
            try:
                return int(v) if v.strip() else None
            except ValueError:
                return None

        c["params"] = {
            "system": self.p_system.get("1.0", "end").strip(),
            "temperature": round(self.p_vars["temperature"][0].get(), 2),
            "top_p": round(self.p_vars["top_p"][0].get(), 2),
            "num_ctx": num(self.p_vars["num_ctx"][0].get()),
            "max_tokens": num(self.p_vars["max_tokens"][0].get()),
        }
        self.store.save_chats()

    # model picker
    def render_model_box(self):
        values, labels = [], []
        for cn in self.store.settings["connections"]:
            for m in self.models.get(cn["id"]) or []:
                values.append(cn["id"] + "::" + m["name"])
                extra = " · " + m["params"] if m.get("params") else ""
                labels.append("%s%s   (%s)" % (m["name"], extra, cn["name"]))
        cur = self.current_model()
        if cur and cur not in values:
            cid, name = split_model(cur)
            values.insert(0, cur)
            labels.insert(0, "%s (unavailable)" % (name or cur))
        self.model_values = values
        self.model_box.configure(values=labels or ["No models found"])
        if cur in values:
            self.model_box.current(values.index(cur))
        elif values:
            self.model_box.current(0)
        else:
            self.model_var.set("No models found")

    def current_model(self):
        c = self.active()
        if c and c.get("model"):
            return c["model"]
        i = self.model_box.current() if hasattr(self, "model_box") else -1
        if 0 <= i < len(self.model_values):
            return self.model_values[i]
        return self.store.settings.get("last_model", "")

    def on_model_pick(self, _e=None):
        i = self.model_box.current()
        if 0 <= i < len(self.model_values):
            v = self.model_values[i]
            self.store.settings["last_model"] = v
            self.store.save_settings()
            c = self.active()
            if c:
                c["model"] = v
                self.store.save_chats()

    # transcript
    def render_chat(self):
        for w in self.msg_frame.winfo_children():
            w.destroy()
        self.scroll_canvas.delete("empty")
        self.empty_items = None
        self.live_view = None
        c = self.active()
        self.render_title()
        self.render_browser_chip()
        self.fill_params()
        self.render_model_box()
        self.update_send_btn()
        empty = not c or not c["messages"]
        self.scroll_canvas.itemconfigure(self.msg_win, state="hidden" if empty else "normal")
        if empty:
            self.draw_empty()
            return
        live = self.stream and self.stream["chat_id"] == c["id"]
        for i, m in enumerate(c["messages"]):
            is_live = live and i == len(c["messages"]) - 1
            mv = MessageView(self, self.msg_frame, m, i, live=is_live)
            mv.pack(fill="x", pady=(0, 16))
            if is_live:
                self.live_view = mv
        self.root.after(30, self.scroll_bottom)

    def draw_empty(self):
        c, th, f = self.scroll_canvas, self.th, self.f
        c.delete("empty")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10:
            w, h = 900, 500
        cx, cy = w / 2, h / 2 - 60
        c.create_image(cx, cy, image=self.enso, tags="empty")
        c.create_text(cx, cy + 96, text=self.spaced("Inkelus"), font=(f["title"][0], 22, "bold"),
                      fill=th["text"], tags="empty")
        c.create_text(cx, cy + 128, text="L O C A L   M O D E L S   ·   墨", font=f["sub"], fill=th["dim"], tags="empty")
        c.create_line(cx - 40, cy + 146, cx + 40, cy + 146, fill=th["line_2"], tags="empty")
        c.create_text(cx, cy + 172, text="Pick a model above and start writing. Turn on Web to let it search and "
                                         "read pages in a sandbox.", font=f["body"], fill=th["dim"], tags="empty", width=520,
                      justify="center")
        self.empty_items = True

    def scroll_bottom(self):
        self.update_scrollregion()
        self.scroll_canvas.yview_moveto(1.0)

    def update_send_btn(self):
        busy = bool(self.stream and self.stream["chat_id"] == self.active_id)
        self.send_btn.set("Stop" if busy else "Send", alt=busy)

    def message_action(self, act, i):
        c = self.active()
        if not c or self.stream:
            return
        m = c["messages"][i]
        if act == "copy":
            self.root.clipboard_clear()
            self.root.clipboard_append(m.get("error") or m.get("content") or "")
            self.toast("Copied")
        elif act == "delete":
            del c["messages"][i]
            self.store.save_chats()
            self.render_chat()
        elif act == "edit":
            self._clear_placeholder()
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", m.get("content", ""))
            self.pending_images = [("image", b) for b in m.get("images") or []]
            self.render_attachments()
            del c["messages"][i:]
            self.store.save_chats()
            self.render_chat()
            self.autosize()
            self.prompt.focus_set()
        elif act == "regen":
            # drop this reply and anything after, back to the user turn
            j = i
            while j > 0 and c["messages"][j - 1]["role"] != "user":
                j -= 1
            del c["messages"][j:]
            m = self.current_model()
            if m:
                c["model"] = m
            self.start_generation(c)

    # attachments
    def attach(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="Attach images",
                                            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"),
                                                       ("All files", "*.*")])
        for p in paths:
            try:
                with open(p, "rb") as fh:
                    self.pending_images.append((os.path.basename(p), base64.b64encode(fh.read()).decode()))
            except OSError as e:
                self.toast(str(e), bad=True)
        self.render_attachments()

    def render_attachments(self):
        th, f = self.th, self.f
        for w in self.attach_row.winfo_children():
            w.destroy()
        for i, (name, _b) in enumerate(self.pending_images):
            chip = tk.Label(self.attach_row, text="📎 %s  ✕" % name, font=f["small"], bg=th["panel_2"],
                            fg=th["dim"], padx=8, pady=3, cursor="hand2", highlightthickness=1,
                            highlightbackground=th["line"])
            chip.pack(side="left", padx=(0, 6), pady=(10, 0))
            chip.bind("<Button-1>", lambda e, k=i: (self.pending_images.pop(k), self.render_attachments()))

    # ── Sending and the tool loop ───────────────────────────────────────
    def send(self):
        if self.stream:
            if self.stream["chat_id"] == self.active_id:
                self.stream["cancel"].set()
            else:
                self.toast("Another chat is still generating.")
            return
        text = self.prompt_text()
        if not text and not self.pending_images:
            return
        model = self.current_model()
        if not model or not split_model(model)[0]:
            self.toast("No model selected — check Settings › Servers, or pull one in Models.", bad=True)
            return
        c = self.active() or self.new_chat(model)
        c["model"] = model
        self.store.settings["last_model"] = model
        self.store.save_settings()
        c["messages"].append({"role": "user", "content": text, "images": [b for _n, b in self.pending_images]})
        if not c.get("titled"):
            t = " ".join(text.split()) or "Image"
            c["title"] = t if len(t) <= 48 else t[:46] + "…"
        self.pending_images = []
        self.render_attachments()
        self.prompt.delete("1.0", "end")
        self.autosize()
        self.start_generation(c)

    def start_generation(self, c):
        cid, model = split_model(c["model"])
        conn = self.store.conn(cid)
        if not conn:
            self.toast("That model's server was removed. Pick another model.", bad=True)
            return
        cancel = threading.Event()
        self.stream = {"chat_id": c["id"], "cancel": cancel}
        c["updated"] = time.time()
        self.render_convos()
        self.stats_lab.configure(text="Generating…")
        threading.Thread(target=self.worker, args=(c, conn, model, cancel), daemon=True).start()
        self.render_chat()

    def post(self, *event):
        self.q.put(event)

    def worker(self, c, conn, model, cancel):
        """Runs on a thread: model turn → tool calls → tool results → … → answer."""
        prov = providers.create(conn)
        bs = self.store.settings["web"]
        tools = None
        try:
            if c.get("browser"):
                tools = web.TOOLS if bs.get("allow_search", True) else \
                    [t for t in web.TOOLS if t["function"]["name"] != "web_search"]
                box = self.sandboxes.setdefault(c["id"], web.Sandbox())
                box.allow_private = bool(bs.get("allow_private"))
                box.allow_search = bool(bs.get("allow_search", True))
            steps = int(bs.get("max_steps") or 12)
            for step in range(steps + 1):
                history = []
                system = (c["params"].get("system") or "").strip()
                if tools:
                    system = (system + "\n\n" if system else "") + web.SYSTEM_PROMPT
                if system:
                    history.append({"role": "system", "content": system})
                history += [m for m in c["messages"] if not m.get("error") and m["role"] != "note"]
                reply = {"role": "assistant", "content": "", "thinking": "", "model": c["model"]}
                c["messages"].append(reply)
                self.post("rerender", c["id"])

                def on_delta(text, think, r=reply):
                    r["content"] += text
                    if think:
                        r["thinking"] += think
                    self.post("delta", c["id"])

                calls, stats = prov.chat(model, history, c["params"], on_delta, cancel,
                                         tools=tools if step < steps else None,
                                         keep_alive=self.store.settings["defaults"].get("keep_alive") or None)
                reply["stats"] = stats
                if not calls:
                    if not reply["content"].strip() and not reply["thinking"]:
                        reply["content"] = "_(empty reply)_"
                    self.post("done", c["id"], stats)
                    return
                reply["tool_calls"] = calls
                for call in calls:
                    if cancel.is_set():
                        raise net.Cancelled()
                    res = {"role": "tool", "name": call["name"], "tool_call_id": call["id"],
                           "arguments": call["arguments"], "content": "", "status": "running"}
                    c["messages"].append(res)
                    self.post("rerender", c["id"])
                    if call["name"] not in web.TOOL_NAMES or not tools:
                        res.update(content="Unknown tool %s." % call["name"], status="error", is_error=True)
                        continue
                    if bs.get("confirm", True) and not c.get("allow_all_tools") and not self.ask_tool(c, call, cancel):
                        res.update(content="The user declined this web action.", status="denied", is_error=True)
                        continue
                    try:
                        text, is_err = box.run(call["name"], call["arguments"]), False
                    except Exception as e:  # report tool failures back to the model
                        text, is_err = "Tool error: %s" % e, True
                    if len(text) > TOOL_RESULT_LIMIT:
                        text = text[:TOOL_RESULT_LIMIT] + "\n…[truncated %d chars]" % (len(text) - TOOL_RESULT_LIMIT)
                    res.update(content=text, status="error" if is_err else "done", is_error=is_err)
                    self.post("rerender", c["id"])
            self.post("done", c["id"], None)
        except net.Cancelled:
            last = c["messages"][-1] if c["messages"] else None
            if last and last["role"] == "assistant":
                last["content"] += ("\n\n" if last["content"] else "") + "_— stopped —_"
            elif last and last["role"] == "tool" and last.get("status") == "running":
                last.update(status="cancelled", content="Cancelled by the user.", is_error=True)
            self.post("done", c["id"], "stopped")
        except Exception as e:
            last = c["messages"][-1] if c["messages"] else None
            if last and last["role"] == "assistant" and not last["content"] and not last.get("tool_calls"):
                last["error"] = str(e)
            else:
                c["messages"].append({"role": "assistant", "content": "", "error": str(e), "model": c["model"]})
            self.post("done", c["id"], "error")
            self.post("toast", str(e), True)

    def ask_tool(self, c, call, cancel):
        """Ask on the Tk thread whether to run a browser action; blocks the worker."""
        ev, box = threading.Event(), {}
        self.post("confirm", c["id"], call, ev, box)
        while not ev.wait(0.2):
            if cancel.is_set():
                raise net.Cancelled()
        if box.get("all"):
            c["allow_all_tools"] = True
        return box.get("ok", False)

    def show_confirm(self, cid, call, ev, box):
        th, f = self.th, self.f
        d = tk.Toplevel(self.root, bg=th["panel_2"])
        d.title("Web action")
        d.transient(self.root)
        d.resizable(False, False)
        tk.Frame(d, bg=th["accent"], height=3, width=34).place(x=18, y=0)
        body = tk.Frame(d, bg=th["panel_2"])
        body.pack(padx=24, pady=20, fill="both")
        tk.Label(body, text=self.title_text("Allow web action?"), font=f["h2"], bg=th["panel_2"],
                 fg=th["text"]).pack(anchor="w")
        tk.Label(body, text="The model wants to use the sandbox browser (read-only):", font=f["label"], bg=th["panel_2"],
                 fg=th["dim"]).pack(anchor="w", pady=(6, 8))
        tk.Label(body, text=call["name"], font=(f["mono"][0], f["mono"][1] + 1, "bold"), bg=th["panel_2"],
                 fg=th["accent_2"]).pack(anchor="w")
        args = json.dumps(call["arguments"], indent=2, ensure_ascii=False)
        t = tk.Text(body, height=min(12, args.count("\n") + 2), width=64, bg=th["code_bg"], fg=th["text"],
                    font=f["mono"], relief="flat", padx=10, pady=8, highlightthickness=0)
        t.insert("1.0", args[:4000])
        t.configure(state="disabled")
        t.pack(fill="x", pady=(6, 14))
        row = tk.Frame(body, bg=th["panel_2"])
        row.pack(fill="x")

        def answer(ok, all_=False):
            box["ok"], box["all"] = ok, all_
            ev.set()
            d.destroy()

        Btn(row, th, f, "Deny", lambda: answer(False), kind="danger").pack(side="left")
        Btn(row, th, f, "Allow", lambda: answer(True), kind="primary").pack(side="right")
        Btn(row, th, f, "Allow all in this chat", lambda: answer(True, True)).pack(side="right", padx=8)
        d.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        d.bind("<Escape>", lambda e: answer(False))
        d.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - d.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - d.winfo_height()) // 3
        d.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        d.grab_set()
        d.focus_set()

    # ── Queue pump (Tk thread) ──────────────────────────────────────────
    def poll(self):
        dirty = False
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "delta":
                    dirty = dirty or ev[1] == self.active_id
                elif kind == "rerender":
                    if ev[1] == self.active_id:
                        self.render_chat()
                elif kind == "done":
                    self.finish(ev[1], ev[2])
                elif kind == "confirm":
                    self.show_confirm(*ev[1:])
                elif kind == "toast":
                    self.toast(ev[1], bad=ev[2])
                elif kind == "call":
                    ev[1](*ev[2:])
        except queue.Empty:
            pass
        if dirty and time.time() - self._last_paint > 0.06:
            self._last_paint = time.time()
            self.paint_live()
        elif dirty:
            self.q.put(("delta", self.active_id))
        self.root.after(30, self.poll)

    def paint_live(self):
        mv = self.live_view
        if not mv or not mv.winfo_exists() or not hasattr(mv, "text"):
            self.render_chat()
            return
        c = self.scroll_canvas
        stick = c.yview()[1] > 0.97
        mv.fill(live=True)
        if stick:
            self.root.after(10, self.scroll_bottom)

    def finish(self, cid, info):
        self.stream = None
        c = self.chat_by_id(cid)
        if c:
            c["updated"] = time.time()
            c.pop("allow_all_tools", None)
        self.store.save_chats()
        self.render_convos()
        if cid == self.active_id:
            self.render_chat()
        if isinstance(info, dict):
            label = stats_text(info)
        else:
            label = "Stopped" if info == "stopped" else ""
        self.stats_lab.configure(text=label)
        self.update_send_btn()

    # ── Status / models refresh ─────────────────────────────────────────
    def check_status(self):
        s = self.store.settings
        conn = self.store.conn(s.get("default_conn")) or (s["connections"][0] if s["connections"] else None)

        def run():
            if not conn:
                self.post("call", self.set_status, False, "No server configured")
                return
            try:
                v = providers.create(conn).ping()
                label = v if v.split(" ")[0] == conn["name"] else "%s · %s" % (conn["name"], v)
                self.post("call", self.set_status, True, label)
            except Exception:
                self.post("call", self.set_status, False, "%s offline" % conn["name"])

        threading.Thread(target=run, daemon=True).start()
        self.root.after(30000, self.check_status)

    def refresh_models(self, then=None):
        conns = list(self.store.settings["connections"])

        def run():
            out = {}
            for cn in conns:
                try:
                    out[cn["id"]] = providers.create(cn).list_models()
                except Exception:
                    out[cn["id"]] = None
            self.post("call", self._models_loaded, out, then)

        threading.Thread(target=run, daemon=True).start()

    def _models_loaded(self, out, then):
        self.models.update(out)
        self.render_model_box()
        if then:
            then()

    # ── Models view ─────────────────────────────────────────────────────
    def build_models(self):
        th, f = self.th, self.f
        v = tk.Frame(self.main, bg=th["bg"])
        _lab, tools = self.page_head(v, "Models", "Armory · 武器庫")
        pick = tk.Frame(tools, bg=th["panel_2"], highlightthickness=1, highlightbackground=th["line_2"])
        pick.pack(side="left", padx=(0, 8))
        tk.Label(pick, text="SERVER", font=f["sub"], bg=th["panel_2"], fg=th["faint"]).pack(side="left", padx=(12, 4))
        self.mconn_var = tk.StringVar()
        self.mconn_box = ttk.Combobox(pick, textvariable=self.mconn_var, state="readonly", width=20, font=f["body"])
        self.mconn_box.pack(side="left", padx=(0, 4), pady=2)
        self.mconn_box.bind("<<ComboboxSelected>>", lambda e: self.load_models_view())
        Btn(tools, th, f, "Refresh", self.load_models_view).pack(side="left")

        sf = ScrollFrame(v, th)
        sf.pack(fill="both", expand=True, padx=(28, 12), pady=(0, 18))
        body = sf.inner

        self.pull_panel = Panel(body, th, "Pull a model", f)
        self.pull_panel.pack(fill="x", pady=(0, 16), padx=(0, 16))
        row = tk.Frame(self.pull_panel.body, bg=th["panel"])
        row.pack(fill="x")
        self.pull_var = tk.StringVar()
        pe = entry(row, th, f, textvariable=self.pull_var)
        pe.pack(side="left", fill="x", expand=True, ipady=5)
        pe.bind("<Return>", lambda e: self.pull())
        self.pull_btn = Btn(row, th, f, "Pull", self.pull, kind="primary")
        self.pull_btn.pack(side="left", padx=(8, 0))
        self.pull_bar = ttk.Progressbar(self.pull_panel.body, style="Ink.Horizontal.TProgressbar", maximum=100)
        self.pull_status = tk.Label(self.pull_panel.body, text="e.g. llama3.2, qwen2.5:7b, mistral-nemo",
                                    font=f["small"], bg=th["panel"], fg=th["faint"], anchor="w")
        self.pull_status.pack(fill="x", pady=(8, 0))

        def table(title, sub, cols, height):
            hdr = tk.Frame(body, bg=th["bg"])
            hdr.pack(fill="x", pady=(4, 8))
            tk.Label(hdr, text=self.title_text(title), font=f["h2"], bg=th["bg"], fg=th["text"]).pack(side="left")
            tk.Label(hdr, text=sub.upper(), font=f["sub"], bg=th["bg"], fg=th["dim"]).pack(side="left", padx=12)
            panel = tk.Frame(body, bg=th["panel_2"], highlightthickness=1, highlightbackground=th["line"])
            panel.pack(fill="x", pady=(0, 6), padx=(0, 16))
            tv = ttk.Treeview(panel, columns=[c[0] for c in cols], show="headings", height=height, selectmode="browse")
            for key, label, w, anchor in cols:
                tv.heading(key, text=label, anchor="w")
                tv.column(key, width=w, anchor=anchor, stretch=key == "name")
            tv.pack(fill="x")
            acts = tk.Frame(body, bg=th["bg"])
            acts.pack(fill="x", pady=(4, 18))
            return hdr, panel, tv, acts

        self.run_hdr, self.run_panel, self.run_tv, run_acts = table(
            "In memory", "Running",
            [("name", "Model", 260, "w"), ("params", "Size", 80, "w"), ("vram", "Memory", 150, "w"),
             ("ctx", "Context", 90, "w"), ("expires", "Status", 140, "w")], 3)
        self.run_acts = run_acts
        Btn(run_acts, th, f, "Chat", lambda: self.model_action("chat", self.run_tv), kind="primary",
            small=True).pack(side="left")
        Btn(run_acts, th, f, "Unload", lambda: self.model_action("unload", self.run_tv), small=True).pack(
            side="left", padx=6)

        _h, _p, self.lib_tv, lib_acts = table(
            "Installed", "Library",
            [("name", "Model", 260, "w"), ("params", "Size", 80, "w"), ("quant", "Quant", 90, "w"),
             ("family", "Family", 90, "w"), ("disk", "On disk", 90, "w"), ("modified", "Modified", 100, "w")], 10)
        self.lib_btns = {}
        for act, label, kind in (("chat", "Chat", "primary"), ("load", "Load", ""), ("info", "Info", ""),
                                 ("delete", "Delete", "danger")):
            b = Btn(lib_acts, th, f, label, lambda a=act: self.model_action(a, self.lib_tv), kind=kind, small=True)
            b.pack(side="left", padx=(0, 6))
            self.lib_btns[act] = b
        self.lib_tv.bind("<Double-1>", lambda e: self.model_action("chat", self.lib_tv))
        return v

    def models_conn(self):
        conns = self.store.settings["connections"]
        names = [c["name"] for c in conns]
        self.mconn_box.configure(values=names)
        cur = self.mconn_var.get()
        if cur not in names:
            d = self.store.conn(self.store.settings.get("default_conn"))
            self.mconn_var.set(d["name"] if d else (names[0] if names else ""))
        return next((c for c in conns if c["name"] == self.mconn_var.get()), None)

    def load_models_view(self):
        conn = self.models_conn()
        for tv in (self.run_tv, self.lib_tv):
            tv.delete(*tv.get_children())
        if not conn:
            self.lib_tv.insert("", "end", values=("Add a server in Settings.",))
            return
        prov = providers.create(conn)
        caps = prov.caps
        if caps.get("pull"):
            self.pull_panel.pack(fill="x", pady=(0, 16), padx=(0, 16), before=self.run_hdr)
        else:
            self.pull_panel.pack_forget()
        for w in (self.run_hdr, self.run_panel, self.run_acts):
            if caps.get("running"):
                w.pack(fill="x")
            else:
                w.pack_forget()
        if caps.get("running"):  # restore order & spacing
            self.run_hdr.pack_configure(pady=(4, 8))
            self.run_panel.pack_configure(pady=(0, 6), padx=(0, 16))
            self.run_acts.pack_configure(pady=(4, 18))
        for act in ("load", "info", "delete"):
            b = self.lib_btns[act]
            if caps.get({"load": "load", "info": "show", "delete": "remove"}[act]):
                b.pack(side="left", padx=(0, 6))
            else:
                b.pack_forget()
        self.lib_tv.insert("", "end", values=("Loading…",))

        def run():
            try:
                models = prov.list_models()
                running = prov.running() if caps.get("running") else []
                self.post("call", self._fill_models, conn, models, running, None)
            except Exception as e:
                self.post("call", self._fill_models, conn, [], [], str(e))

        threading.Thread(target=run, daemon=True).start()

    def _fill_models(self, conn, models, running, err):
        for tv in (self.run_tv, self.lib_tv):
            tv.delete(*tv.get_children())
        if err:
            self.lib_tv.insert("", "end", values=(err,))
            return
        self.models[conn["id"]] = models
        self.render_model_box()
        loaded = {r["name"] for r in running}
        for r in running:
            if self.run_tv.exists("r:" + r["name"]):
                continue
            mem = fmt_bytes(r.get("vram"))
            if r.get("size") and r.get("vram") is not None:
                mem = "%d%% GPU · %s" % (round(100 * r["vram"] / r["size"]), mem) if r["vram"] < r["size"] else "GPU · " + mem
            self.run_tv.insert("", "end", iid="r:" + r["name"], values=(
                r["name"], r.get("params") or "", mem, r.get("ctx") or "", until(r.get("expires"))))
        if not running:
            self.run_tv.insert("", "end", values=("Nothing loaded right now.",))
        for m in models:
            if self.lib_tv.exists("m:" + m["name"]):
                continue
            name = m["name"] + ("   ● loaded" if m["name"] in loaded else "")
            self.lib_tv.insert("", "end", iid="m:" + m["name"], values=(
                name, m.get("params") or "", m.get("quant") or "", m.get("family") or m.get("owner") or "",
                fmt_bytes(m.get("size")), ago(m.get("modified"))))
        if not models:
            self.lib_tv.insert("", "end", values=("No models installed — pull one above.",))

    def model_action(self, act, tv):
        sel = tv.selection()
        if not sel or ":" not in sel[0]:
            self.toast("Select a model first.")
            return
        name = sel[0].split(":", 1)[1]
        conn = self.models_conn()
        prov = providers.create(conn)
        if act == "chat":
            self.new_chat(conn["id"] + "::" + name)
            return
        if act == "delete" and not messagebox.askyesno(
                "Delete model", "Delete %s from %s? This removes it from disk." % (name, conn["name"]), parent=self.root):
            return
        keep = self.store.settings["defaults"].get("keep_alive")

        def run():
            try:
                if act == "info":
                    self.post("call", self.show_info, name, prov.show(name))
                    return
                if act == "delete":
                    prov.remove(name)
                elif act == "load":
                    self.post("toast", "Loading %s…" % name, False)
                    prov.load(name, keep)
                elif act == "unload":
                    prov.unload(name)
                self.post("toast", {"delete": "Deleted %s", "load": "%s loaded", "unload": "%s unloaded"}[act] % name,
                          False)
            except Exception as e:
                self.post("toast", str(e), True)
            self.post("call", self.load_models_view)

        threading.Thread(target=run, daemon=True).start()

    def show_info(self, name, r):
        d, info = r.get("details") or {}, r.get("model_info") or {}
        ctx = next((v for k, v in info.items() if k.endswith(".context_length")), "")
        rows = [("Family", d.get("family")), ("Parameters", d.get("parameter_size")),
                ("Quantization", d.get("quantization_level")), ("Format", d.get("format")),
                ("Context length", ctx), ("Capabilities", ", ".join(r.get("capabilities") or []))]
        text = "\n".join("%-16s %s" % (k, v) for k, v in rows if v)
        for key in ("parameters", "system", "template", "license"):
            if r.get(key):
                text += "\n\n── %s ──\n%s" % (key.upper(), str(r[key])[:4000])
        self.dialog(name, text)

    def pull(self):
        if self.pull_cancel:
            self.pull_cancel.set()
            return
        name = self.pull_var.get().strip()
        conn = self.models_conn()
        if not name or not conn:
            return
        cancel = self.pull_cancel = threading.Event()
        self.pull_btn.configure(text="Cancel" if self.th["name"] == "ink" else "CANCEL")
        self.pull_bar.pack(fill="x", pady=(12, 0), before=self.pull_status)
        self.pull_bar["value"] = 0
        self.pull_status.configure(text="Starting…")

        def progress(j):
            if j.get("total") and j.get("completed") is not None:
                pct = 100 * j["completed"] / j["total"]
                self.post("call", self._pull_progress, pct, "%s · %s / %s (%d%%)" % (
                    j.get("status", ""), fmt_bytes(j["completed"]), fmt_bytes(j["total"]), pct))
            else:
                self.post("call", self._pull_progress, None, j.get("status", ""))

        def run():
            msg, ok = "%s is ready." % name, True
            try:
                providers.create(conn).pull(name, progress, cancel)
            except net.Cancelled:
                msg, ok = "Cancelled.", False
            except Exception as e:
                msg, ok = str(e), False
            self.post("call", self._pull_done, msg, ok)

        threading.Thread(target=run, daemon=True).start()

    def _pull_progress(self, pct, text):
        if pct is not None:
            self.pull_bar["value"] = pct
        self.pull_status.configure(text=text)

    def _pull_done(self, msg, ok):
        self.pull_cancel = None
        self.pull_btn.configure(text="Pull" if self.th["name"] == "ink" else "PULL")
        self.pull_status.configure(text=msg)
        self.root.after(1500, lambda: self.pull_cancel is None and self.pull_bar.pack_forget())
        if ok:
            self.pull_var.set("")
            self.toast(msg)
            self.load_models_view()

    # ── Settings view ───────────────────────────────────────────────────
    def build_settings(self):
        th, f = self.th, self.f
        v = tk.Frame(self.main, bg=th["bg"])
        self.page_head(v, "Settings", "Configuration · 設定")
        sf = ScrollFrame(v, th)
        sf.pack(fill="both", expand=True, padx=(28, 12), pady=(0, 18))
        body = sf.inner

        def label(parent, text):
            tk.Label(parent, text=text, font=f["label"], bg=th["panel"], fg=th["dim"]).pack(anchor="w", pady=(8, 4))

        def hint(parent, text):
            lab = tk.Label(parent, text=text, font=f["small"], bg=th["panel"], fg=th["faint"], justify="left",
                           anchor="w", wraplength=500)
            lab.pack(fill="x", pady=(6, 0))
            lab.bind("<Configure>", lambda e: lab.configure(wraplength=max(e.width - 6, 200)))
            return lab

        # style
        p = Panel(body, th, "Interface style", f)
        p.pack(fill="x", pady=(0, 16), padx=(0, 16))
        row = tk.Frame(p.body, bg=th["panel"])
        row.pack(anchor="w")
        self.style_tabs = {}
        for key, name in (("ink", "Ink"), ("glass", "Glass")):
            t = Tab(row, th, f, name, lambda k=key: self.set_style(k))
            t.pack(side="left", padx=(0, 6))
            self.style_tabs[key] = t
        hint(p.body, "Ink: charcoal, cream paper and gold with serif type. "
                     "Glass: dark slate tiles, condensed type and a yellow accent.")

        # web sandbox
        p = Panel(body, th, "Web sandbox", f)
        p.pack(fill="x", pady=(0, 16), padx=(0, 16))
        hint(p.body, "Inkelus has its own built-in, read-only browser for the model. It is not BrowserOS and never "
                     "touches any browser on this computer: no cookies or logins, no JavaScript, no forms, "
                     "nothing saved to disk. It can search (DuckDuckGo), open pages, follow links and find text. "
                     "Turn it on per chat with the Web button in the chat header. Needs a model that supports "
                     "tool calling (e.g. qwen2.5, qwen3, llama3.1+, mistral-nemo).")
        self.web_vars = {}
        for key, text in (("confirm", "Ask before each page or search"),
                          ("allow_search", "Allow web search (DuckDuckGo)"),
                          ("allow_private", "Allow local and intranet addresses (localhost, 10.x, 192.168.x…)")):
            var = tk.BooleanVar()
            ttk.Checkbutton(p.body, text=text, variable=var, command=self.save_web).pack(anchor="w", pady=(8, 0))
            self.web_vars[key] = var
        opts = tk.Frame(p.body, bg=th["panel"])
        opts.pack(fill="x", pady=(10, 0))
        tk.Label(opts, text="Max web steps per reply", font=f["label"], bg=th["panel"], fg=th["dim"]).pack(
            side="left", padx=(0, 8))
        self.web_steps = tk.StringVar()
        entry(opts, th, f, width=5, textvariable=self.web_steps).pack(side="left", ipady=2)
        self.web_steps.trace_add("write", lambda *a: self.save_web())
        hint(p.body, "Tools the model gets: " + ", ".join(web.TOOL_NAMES) + ".")

        # servers
        p = Panel(body, th, "Servers", f)
        p.pack(fill="x", pady=(0, 16), padx=(0, 16))
        self.conn_list = tk.Frame(p.body, bg=th["panel"])
        self.conn_list.pack(fill="x", pady=(0, 12))
        form = tk.Frame(p.body, bg=th["panel"])
        form.pack(fill="x")
        self.cf = {k: tk.StringVar() for k in ("id", "name", "type", "url", "key")}
        for col, (k, lab) in enumerate((("name", "Name"), ("type", "Type"))):
            box = tk.Frame(form, bg=th["panel"])
            box.grid(row=0, column=col, sticky="ew", padx=(0, 12 if col == 0 else 0))
            label(box, lab)
            if k == "type":
                cb = ttk.Combobox(box, textvariable=self.cf["type"], state="readonly", font=f["body"],
                                  values=["Ollama", "OpenAI-compatible"])
                cb.pack(fill="x")
                cb.bind("<<ComboboxSelected>>", self.on_conn_type)
            else:
                entry(box, th, f, textvariable=self.cf[k]).pack(fill="x", ipady=5)
        for r, (k, lab) in enumerate((("url", "Base URL"), ("key", "API key (optional)")), start=1):
            box = tk.Frame(form, bg=th["panel"])
            box.grid(row=r, column=0, columnspan=2, sticky="ew")
            label(box, lab)
            entry(box, th, f, textvariable=self.cf[k], show="•" if k == "key" else None).pack(fill="x", ipady=5)
        form.grid_columnconfigure(0, weight=1)
        form.grid_columnconfigure(1, weight=1)
        row = tk.Frame(p.body, bg=th["panel"])
        row.pack(fill="x", pady=(12, 0))
        Btn(row, th, f, "Save server", self.save_conn, kind="primary").pack(side="right")
        Btn(row, th, f, "Clear", lambda: self.fill_conn_form(None)).pack(side="right", padx=8)
        hint(p.body, "Ollama: http://localhost:11434 · LM Studio: http://localhost:1234/v1 · "
                     "llama.cpp: http://localhost:8080/v1 · vLLM: http://localhost:8000/v1")

        # defaults
        p = Panel(body, th, "Defaults for new chats", f)
        p.pack(fill="x", pady=(0, 16), padx=(0, 16))
        label(p.body, "System prompt")
        self.d_system = textbox(p.body, th, f, height=3)
        self.d_system.pack(fill="x")
        self.d_system.bind("<KeyRelease>", lambda e: self.save_defaults())
        label(p.body, "Ollama keep-alive (e.g. 5m, 1h, -1 to keep loaded)")
        self.d_keep = tk.StringVar()
        entry(p.body, th, f, width=14, textvariable=self.d_keep).pack(anchor="w", ipady=4)
        self.d_keep.trace_add("write", lambda *a: self.save_defaults())

        # data
        p = Panel(body, th, "Data", f)
        p.pack(fill="x", pady=(0, 16), padx=(0, 16))
        hint(p.body, "Conversations and settings are saved in %s" % self.store.dir)
        row = tk.Frame(p.body, bg=th["panel"])
        row.pack(fill="x", pady=(10, 0))
        Btn(row, th, f, "Export all", self.export_all).pack(side="left")
        Btn(row, th, f, "Import", self.import_all).pack(side="left", padx=8)
        Btn(row, th, f, "Delete all chats", self.wipe_all, kind="danger").pack(side="left")
        return v

    def fill_settings(self):
        s = self.store.settings
        for k, t in self.style_tabs.items():
            t.set_active(k == s.get("style", "ink"))
        ws = s["web"]
        self._filling_web = True
        for key, var in self.web_vars.items():
            var.set(bool(ws.get(key, key != "allow_private")))
        self.web_steps.set(str(ws.get("max_steps") or 8))
        self._filling_web = False
        self.render_conn_list()
        if not self.cf["type"].get():
            self.fill_conn_form(None)
        self._filling_def = True
        self.d_system.delete("1.0", "end")
        self.d_system.insert("1.0", s["defaults"].get("system", ""))
        self.d_keep.set(s["defaults"].get("keep_alive", ""))
        self._filling_def = False

    def set_style(self, key):
        self.store.settings["style"] = key
        self.store.save_settings()
        self.build()
        self.show_view("settings")

    def save_web(self):
        if getattr(self, "_filling_web", False):
            return
        ws = self.store.settings["web"]
        for key, var in self.web_vars.items():
            ws[key] = bool(var.get())
        try:
            ws["max_steps"] = max(1, min(int(self.web_steps.get()), 30))
        except ValueError:
            pass
        self.store.save_settings()

    def render_conn_list(self):
        th, f = self.th, self.f
        for w in self.conn_list.winfo_children():
            w.destroy()
        s = self.store.settings
        for c in s["connections"]:
            default = c["id"] == s.get("default_conn")
            row = tk.Frame(self.conn_list, bg=th["panel_2"], highlightthickness=1, highlightbackground=th["line"])
            row.pack(fill="x", pady=2)
            tk.Frame(row, bg=th["accent"] if default else th["panel_2"], width=2).pack(side="left", fill="y")
            info = tk.Frame(row, bg=th["panel_2"])
            info.pack(side="top", fill="x", padx=12, pady=(8, 4))
            tk.Label(info, text=c["name"], font=(f["body"][0], f["body"][1], "bold"), bg=th["panel_2"],
                     fg=th["text"]).pack(anchor="w")
            tk.Label(info, text="%s · %s" % ("OpenAI-compatible" if c["type"] == "openai" else "Ollama", c["url"]),
                     font=f["small"], bg=th["panel_2"], fg=th["faint"], anchor="w", width=1).pack(anchor="w", fill="x")
            acts = tk.Frame(row, bg=th["panel_2"])
            acts.pack(side="top", anchor="w", padx=10, pady=(0, 8))
            if default:
                tk.Label(acts, text="default", font=f["small"], bg=th["panel_2"], fg=th["accent_2"], padx=8,
                         highlightthickness=1, highlightbackground=th["line_2"]).pack(side="left", padx=4)
            else:
                Btn(acts, th, f, "Make default", lambda cc=c: self.conn_default(cc), small=True).pack(side="left", padx=2)
            Btn(acts, th, f, "Test", lambda cc=c: self.conn_test(cc), small=True).pack(side="left", padx=2)
            Btn(acts, th, f, "Edit", lambda cc=c: self.fill_conn_form(cc), small=True).pack(side="left", padx=2)
            Btn(acts, th, f, "Remove", lambda cc=c: self.conn_remove(cc), kind="danger", small=True).pack(
                side="left", padx=2)
        if not s["connections"]:
            tk.Label(self.conn_list, text="No servers yet.", font=f["small"], bg=th["panel"], fg=th["faint"]).pack(
                anchor="w")

    def fill_conn_form(self, c):
        self.cf["id"].set(c["id"] if c else "")
        self.cf["name"].set(c["name"] if c else "")
        self.cf["type"].set("OpenAI-compatible" if c and c["type"] == "openai" else "Ollama")
        self.cf["url"].set(c["url"] if c else "")
        self.cf["key"].set(c.get("key", "") if c else "")

    def on_conn_type(self, _e=None):
        u = self.cf["url"].get().strip()
        if not u or u in ("http://localhost:11434", "http://localhost:1234/v1"):
            self.cf["url"].set("http://localhost:1234/v1" if self.cf["type"].get() != "Ollama"
                               else "http://localhost:11434")

    def save_conn(self):
        data = {"name": self.cf["name"].get().strip(), "url": self.cf["url"].get().strip().rstrip("/"),
                "type": "ollama" if self.cf["type"].get() == "Ollama" else "openai", "key": self.cf["key"].get().strip()}
        if not data["name"]:
            self.toast("Give the server a name.", bad=True)
            return
        if not data["url"].startswith(("http://", "https://")):
            self.toast("Base URL must start with http:// or https://", bad=True)
            return
        s = self.store.settings
        cid = self.cf["id"].get()
        if cid and self.store.conn(cid):
            self.store.conn(cid).update(data)
        else:
            data["id"] = new_id()
            s["connections"].append(data)
            if not self.store.conn(s.get("default_conn")):
                s["default_conn"] = data["id"]
        self.store.save_settings()
        self.fill_conn_form(None)
        self.render_conn_list()
        self.refresh_models()
        self.toast("Server saved")

    def conn_default(self, c):
        self.store.settings["default_conn"] = c["id"]
        self.store.save_settings()
        self.render_conn_list()

    def conn_test(self, c):
        def run():
            try:
                self.post("toast", "%s: %s" % (c["name"], providers.create(c).ping()), False)
            except Exception as e:
                self.post("toast", "%s: %s" % (c["name"], e), True)

        threading.Thread(target=run, daemon=True).start()

    def conn_remove(self, c):
        if not messagebox.askyesno("Remove server", "Remove %s? Chats that used it keep their history." % c["name"],
                                   parent=self.root):
            return
        s = self.store.settings
        s["connections"] = [x for x in s["connections"] if x["id"] != c["id"]]
        self.models.pop(c["id"], None)
        if s.get("default_conn") == c["id"]:
            s["default_conn"] = s["connections"][0]["id"] if s["connections"] else None
        self.store.save_settings()
        self.render_conn_list()
        self.render_model_box()

    def save_defaults(self):
        if getattr(self, "_filling_def", False):
            return
        d = self.store.settings["defaults"]
        d["system"] = self.d_system.get("1.0", "end").strip()
        d["keep_alive"] = self.d_keep.get().strip()
        self.store.save_settings()

    # ── Import / export ─────────────────────────────────────────────────
    def export_chat(self, c):
        if not c or not c["messages"]:
            self.toast("Nothing to export yet.")
            return
        safe = "".join(ch for ch in c["title"] if ch.isalnum() or ch in " -_").strip()[:60] or "chat"
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".md", initialfile=safe + ".md",
                                            filetypes=[("Markdown", "*.md")])
        if not path:
            return
        out = ["# " + c["title"], ""]
        for m in c["messages"]:
            if m["role"] == "tool":
                out += ["> ⚙ `%s` %s" % (m.get("name"), json.dumps(m.get("arguments") or {})), ""]
                continue
            who = "You" if m["role"] == "user" else (split_model(m.get("model"))[1] or "Assistant")
            body = "> Error: " + m["error"] if m.get("error") else m.get("content", "")
            if body.strip():
                out += ["### " + who, "", body, ""]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        self.toast("Exported")

    def export_all(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json",
                                            initialfile="inkelus-backup.json", filetypes=[("JSON", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": 2, "chats": self.store.chats}, fh, ensure_ascii=False, indent=1)
            self.toast("Exported %d conversations" % len(self.store.chats))

    def import_all(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            incoming = data if isinstance(data, list) else data.get("chats")
            have = {c["id"] for c in self.store.chats}
            added = [c for c in incoming or [] if isinstance(c, dict) and c.get("id") and
                     isinstance(c.get("messages"), list) and c["id"] not in have]
            for c in added:
                c.setdefault("params", {"system": "", "temperature": 0.7, "top_p": 0.9})
                c.setdefault("updated", time.time())
                c.setdefault("title", "Imported")
                # the web version stored images as data: URLs
                for m in c["messages"]:
                    m["images"] = [i.split(",", 1)[-1] for i in m.get("images") or []]
            self.store.chats.extend(added)
            self.store.save_chats()
            self.render_convos()
            self.toast("Imported %d conversation%s" % (len(added), "" if len(added) == 1 else "s"))
        except (OSError, ValueError, AttributeError) as e:
            self.toast("Import failed: %s" % e, bad=True)

    def wipe_all(self):
        if not messagebox.askyesno("Delete all chats", "Delete every conversation? This cannot be undone.",
                                   parent=self.root):
            return
        if self.stream:
            self.stream["cancel"].set()
        self.store.chats.clear()
        self.active_id = None
        self.store.save_chats()
        self.render_convos()
        self.render_chat()

    # ── Little UI helpers ───────────────────────────────────────────────
    def toast(self, msg, bad=False):
        th, f = self.th, self.f
        if getattr(self, "_toast", None) and self._toast.winfo_exists():
            self._toast.destroy()
        t = self._toast = tk.Label(self.root, text=msg, font=f["label"], bg=th["panel_2"],
                                   fg=th["red"] if bad else th["text"], padx=18, pady=8, highlightthickness=1,
                                   highlightbackground=th["red"] if bad else th["line_2"], wraplength=600)
        t.place(relx=0.5, y=14, anchor="n")
        t.after(5000 if bad else 2600, lambda: t.winfo_exists() and t.destroy())

    def dialog(self, title, text):
        th, f = self.th, self.f
        d = tk.Toplevel(self.root, bg=th["panel_2"])
        d.title(title)
        d.transient(self.root)
        d.geometry("680x520")
        tk.Frame(d, bg=th["accent"], height=3, width=34).place(x=18, y=0)
        tk.Label(d, text=self.title_text(title), font=f["h2"], bg=th["panel_2"], fg=th["text"]).pack(
            anchor="w", padx=24, pady=(20, 10))
        t = tk.Text(d, bg=th["code_bg"], fg=th["text"], font=f["mono"], relief="flat", padx=12, pady=10,
                    highlightthickness=0, wrap="word")
        t.insert("1.0", text)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True, padx=24)
        Btn(d, th, f, "Close", d.destroy).pack(anchor="e", padx=24, pady=16)
        d.bind("<Escape>", lambda e: d.destroy())

    def run(self):
        self.root.bind_all("<Control-n>", lambda e: self.new_chat())
        self.root.bind_all("<Escape>", lambda e: self.stream and self.stream["cancel"].set())
        self.root.mainloop()


def main():
    App().run()
