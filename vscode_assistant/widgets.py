"""Custom Tk widgets that carry the Professional / Ink / Glass looks."""
import sys
import tkinter as tk
from tkinter import ttk

from .theme import case, mix


class Slip(tk.Canvas):
    """The main action. Ink: a cream paper slip with a clipped corner, Cinzel
    italic label, a rule and an arrow (NXBNVNB's ENTER button). Glass: a
    yellow pill. Professional: a flat blue button with a centred label."""

    def __init__(self, master, theme, fonts, text, command, sub="", width=200, height=46):
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0,
                         bg=master["bg"], cursor="hand2")
        self.theme, self.fonts, self.text, self.sub, self.command = theme, fonts, text, sub, command
        self.alt = False
        self.enabled = True
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self.draw(hover=True))
        self.bind("<Leave>", lambda e: self.draw())

    def _click(self, _e):
        if self.enabled and self.command:
            self.command()

    def set(self, text=None, alt=None):
        if text is not None:
            self.text = text
        if alt is not None:
            self.alt = alt
        self.draw()

    def draw(self, hover=False):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            w, h = int(self["width"]), int(self["height"])
        th = self.theme
        if th["name"] == "pro":
            self._draw_flat(w, h, hover)
            return
        if self.alt:  # "Stop": dark ink slip with gold text
            top, bot, fg = "#2a2620", "#1a1712", th["accent_2"]
            if th["name"] == "glass":
                top, bot, fg = th["sel"], th["sel"], th["on_sel"]
        else:
            top, bot, fg = th["slip_top"], th["slip_bot"], th["on_accent"]
        if hover:
            top, bot = mix(top, "#ffffff", 0.08), mix(bot, "#ffffff", 0.08)
        if th["name"] == "ink":
            for y in range(h):  # vertical paper gradient
                self.create_line(0, y, w, y, fill=mix(top, bot, y / max(h - 1, 1)))
            cut = 10  # clipped bottom-left corner
            self.create_polygon(0, h - cut, cut, h, 0, h, fill=self["bg"], outline="")
            self.create_line(0, 0, w - 1, 0, w - 1, h - 1, cut, h - 1, 0, h - cut - 1, 0, 0,
                             fill="#9d8550" if not self.alt else th["line_2"])
        else:
            r = h // 2
            self.create_oval(0, 0, h, h - 1, fill=top, outline="")
            self.create_oval(w - h, 0, w - 1, h - 1, fill=top, outline="")
            self.create_rectangle(r, 0, w - r, h - 1, fill=top, outline="")
        x = 20 if th["name"] == "ink" else 22
        label = self.text if th["name"] == "ink" else self.text.upper()
        t = self.create_text(x, h / 2 - (3 if self.sub and th["name"] == "ink" else 0), text=label,
                             anchor="w", font=self.fonts["slip"], fill=fg)
        x2 = self.bbox(t)[2] + 12
        self.create_line(x2, h / 2, w - 36, h / 2, fill=fg)
        self.create_text(w - 18, h / 2, text="→", font=(self.fonts["body"][0], 13), fill=fg)
        if self.sub and th["name"] == "ink":
            self.create_text(w - 40, h - 7, text=self.sub, anchor="e", font=(self.fonts["body"][0], 7, "italic"),
                             fill="#4a3f29")
        if not self.enabled:
            self.create_rectangle(0, 0, w, h, fill=self["bg"], stipple="gray50", outline="")

    def _draw_flat(self, w, h, hover):
        th = self.theme
        if self.alt:  # "Stop": a quiet secondary button
            fill, line, fg = th["panel_hi"], th["line_2"], th["text"]
        else:
            fill, line, fg = th["accent"], th["accent"], th["on_accent"]
        if hover:
            fill = mix(fill, "#ffffff", 0.1)
        self.create_rectangle(0, 0, w - 1, h - 1, fill=fill, outline=line)
        self.create_text(w / 2, h / 2, text=self.text, font=self.fonts["slip"], fill=fg)
        if not self.enabled:
            self.create_rectangle(0, 0, w, h, fill=self["bg"], stipple="gray50", outline="")


class NavItem(tk.Canvas):
    """A rail row. Active in Ink: gold wash fading right, 2px gold edge,
    hairlines top and bottom, a gold diamond on the left. Active in Glass: a
    white block with dark text. Active in Professional: a grey row with a
    blue bar on the left edge."""

    ICONS = {
        "chat": lambda c, x, y, col: (c.create_line(x, y - 6, x + 14, y - 6, x + 14, y + 4, x + 5, y + 4, x, y + 8,
                                                    x, y - 6, fill=col, width=1.4)),
        "models": lambda c, x, y, col: (c.create_polygon(x + 7, y - 8, x + 14, y - 4, x + 14, y + 4, x + 7, y + 8,
                                                         x, y + 4, x, y - 4, outline=col, fill="", width=1.4),
                                        c.create_line(x, y - 4, x + 7, y, x + 14, y - 4, fill=col, width=1.2),
                                        c.create_line(x + 7, y, x + 7, y + 8, fill=col, width=1.2)),
        "library": lambda c, x, y, col: (c.create_rectangle(x + 1, y - 8, x + 12, y + 7, outline=col, width=1.4),
                                         c.create_line(x + 4, y - 3, x + 9, y - 3, fill=col, width=1.2),
                                         c.create_line(x + 4, y + 1, x + 9, y + 1, fill=col, width=1.2),
                                         c.create_line(x + 15, y - 5, x + 15, y + 10, x + 4, y + 10,
                                                       fill=col, width=1.4)),
        "settings": lambda c, x, y, col: (c.create_oval(x + 3, y - 4, x + 11, y + 4, outline=col, width=1.4),
                                          [c.create_line(x + 7 + dx * 5, y + dy * 5, x + 7 + dx * 8, y + dy * 8,
                                                         fill=col, width=1.4)
                                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (.7, .7), (-.7, -.7),
                                                          (.7, -.7), (-.7, .7))]),
    }

    def __init__(self, master, theme, fonts, key, label, command, height=44):
        super().__init__(master, height=height, highlightthickness=0, bd=0, bg=master["bg"], cursor="hand2")
        self.theme, self.fonts, self.key, self.label, self.command = theme, fonts, key, label, command
        self.active = False
        self.hover = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", lambda e: command(key))
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def _hover(self, on):
        self.hover = on
        self.draw()

    def set_active(self, on):
        self.active = on
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        th, bg = self.theme, self["bg"]
        col = th["dim"] if not self.hover else th["text"]
        ink = th["name"] == "ink"
        if self.active and ink:
            for x in range(0, w, 2):
                t = min(x / (w * 0.8), 1)
                self.create_line(x, 0, x, h, fill=mix("#4b3c1e", bg, t), width=2)
            self.create_line(0, 0, w, 0, fill=mix("#6b5a34", bg, 0.3))
            self.create_line(0, h - 1, w, h - 1, fill=mix("#6b5a34", bg, 0.3))
            self.create_rectangle(0, 0, 1, h, fill=th["accent"], outline="")
            self.create_polygon(0, h / 2 - 5, 5, h / 2, 0, h / 2 + 5, fill=th["accent"], outline="")
            col = "#fff6dc"
        elif self.active and th["name"] == "pro":
            self.create_rectangle(0, 0, w, h, fill=th["wash"], outline="")
            self.create_rectangle(0, 0, 1, h, fill=th["accent"], outline="")
            col = th["text"]
        elif self.active:
            self.create_rectangle(12, 3, w - 12, h - 3, fill=th["sel"], outline="")
            col = th["on_sel"]
        elif self.hover and th["name"] == "pro":
            self.create_rectangle(0, 0, w, h, fill=mix(bg, "#ffffff", 0.04), outline="")
        x = 26
        self.ICONS[self.key](self, x, h / 2, col)
        label = case(th, self.label)
        self.create_text(x + 28, h / 2, text=label, anchor="w", font=self.fonts["nav"], fill=col)


class Btn(tk.Label):
    """A flat button: dark with a hairline (default), paper/yellow (primary),
    red text (danger)."""

    def __init__(self, master, theme, fonts, text, command, kind="", small=False, **kw):
        th = theme
        bg, fg, line = th["panel_2"], th["text"], th["line_2"]
        if kind == "primary":
            bg, fg, line = th["slip_bot"] if th["name"] == "ink" else th["accent"], th["on_accent"], th["accent"]
        elif kind == "danger":
            fg = th["red"]
        super().__init__(master, text=case(th, text), bg=bg, fg=fg,
                         font=fonts["btn"] if not small else fonts["label"], padx=12 if small else 16,
                         pady=3 if small else 6, cursor="hand2", highlightthickness=1,
                         highlightbackground=line, highlightcolor=line, **kw)
        self._bg = bg
        self.command = command
        self.enabled = True
        self.bind("<Button-1>", lambda e: self.enabled and self.command and self.command())
        self.bind("<Enter>", lambda e: self.configure(bg=mix(self._bg, "#ffffff", 0.1)))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))

    def set_enabled(self, on):
        self.enabled = on
        self.configure(fg=self["fg"] if on else self.master["bg"])


class Tab(tk.Label):
    def __init__(self, master, theme, fonts, text, command):
        self.theme = theme
        super().__init__(master, text=case(theme, text), font=fonts["btn"],
                         padx=18, pady=5, cursor="hand2", highlightthickness=1)
        self.bind("<Button-1>", lambda e: command())
        self.set_active(False)

    def set_active(self, on):
        th = self.theme
        if on:
            self.configure(bg=th["sel"], fg=th["on_sel"],
                           highlightbackground=th["accent"] if th["name"] == "pro" else th["sel"])
        else:
            self.configure(bg=th["panel_2"], fg=th["dim"], highlightbackground=th["line"])


class Panel(tk.Frame):
    """A dark tile with a hairline and (Ink / Glass) the short accent tick top-left."""

    def __init__(self, master, theme, title=None, fonts=None, **kw):
        super().__init__(master, bg=theme["panel"], highlightthickness=1, highlightbackground=theme["line"], **kw)
        if theme["ticks"]:
            tk.Frame(self, bg=theme["accent"], height=2, width=24).place(x=14, y=0)
        self.body = tk.Frame(self, bg=theme["panel"])
        self.body.pack(fill="both", expand=True, padx=18, pady=(14, 16))
        if title:
            tk.Label(self.body, text=case(theme, title), font=fonts["h2"],
                     bg=theme["panel"], fg=theme["text"]).pack(anchor="w", pady=(0, 10))


class ScrollFrame(tk.Frame):
    """A vertically scrolling frame; .inner is where children go."""

    def __init__(self, master, theme, bg=None, **kw):
        bg = bg or master["bg"]
        super().__init__(master, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Ink.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        bind_wheel(self.canvas, self.canvas)
        bind_wheel(self.inner, self.canvas)


def bind_wheel(widget, canvas):
    """Scroll `canvas` with the mouse wheel while the pointer is over `widget`."""

    def on_wheel(e):
        if canvas.yview() == (0.0, 1.0):
            return
        if e.num == 4:
            canvas.yview_scroll(-3, "units")
        elif e.num == 5:
            canvas.yview_scroll(3, "units")
        else:
            d = e.delta if sys.platform == "darwin" else e.delta // 120
            canvas.yview_scroll(-d * 3, "units")

    def enter(_e):
        widget.bind_all("<MouseWheel>", on_wheel)
        widget.bind_all("<Button-4>", on_wheel)
        widget.bind_all("<Button-5>", on_wheel)

    def leave(_e):
        widget.unbind_all("<MouseWheel>")
        widget.unbind_all("<Button-4>")
        widget.unbind_all("<Button-5>")

    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", leave, add="+")


def style_ttk(root, theme, fonts):
    s = ttk.Style(root)
    s.theme_use("clam")
    th = theme
    s.configure(".", background=th["bg"], foreground=th["text"], fieldbackground=th["input"],
                bordercolor=th["line_2"], lightcolor=th["line"], darkcolor=th["line"],
                troughcolor=th["panel_2"], font=fonts["body"], arrowcolor=th["dim"])
    s.configure("Ink.Vertical.TScrollbar", background=th["panel_2"], troughcolor=th["bg"], bordercolor=th["bg"],
                arrowcolor=th["faint"], lightcolor=th["panel_2"], darkcolor=th["panel_2"], gripcount=0, arrowsize=10)
    s.map("Ink.Vertical.TScrollbar", background=[("active", th["line_2"])])
    s.configure("TCombobox", fieldbackground=th["input"], background=th["panel_2"], foreground=th["text"],
                selectbackground=th["input"], selectforeground=th["text"], arrowcolor=th["accent"], padding=5)
    s.map("TCombobox", fieldbackground=[("readonly", th["input"])], foreground=[("readonly", th["text"])],
          selectbackground=[("readonly", th["input"])], selectforeground=[("readonly", th["text"])])
    root.option_add("*TCombobox*Listbox.background", th["panel_2"])
    root.option_add("*TCombobox*Listbox.foreground", th["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", th["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", th["on_accent"])
    root.option_add("*TCombobox*Listbox.font", fonts["body"])
    s.configure("Treeview", background=th["panel_2"], fieldbackground=th["panel_2"], foreground=th["text"],
                rowheight=int(fonts["body"][1] * 2.6), bordercolor=th["line"], borderwidth=0, font=fonts["body"])
    s.map("Treeview", background=[("selected", th["panel_hi"])], foreground=[("selected", th["accent_2"])])
    s.configure("Treeview.Heading", background=th["panel"], foreground=th["dim"], bordercolor=th["line"],
                lightcolor=th["panel"], darkcolor=th["panel"], relief="flat", font=fonts["label"], padding=6)
    s.map("Treeview.Heading", background=[("active", th["panel_hi"])])
    s.configure("Ink.Horizontal.TProgressbar", troughcolor=th["panel_2"], background=th["accent"],
                bordercolor=th["panel_2"], lightcolor=th["accent"], darkcolor=th["accent"], thickness=4)
    s.configure("Ink.Horizontal.TScale", background=th["accent"], troughcolor=th["panel_2"],
                bordercolor=th["line"], lightcolor=th["accent"], darkcolor=th["accent"])
    s.configure("TCheckbutton", background=th["panel"], foreground=th["dim"], indicatorcolor=th["input"],
                font=fonts["label"])
    s.map("TCheckbutton", indicatorcolor=[("selected", th["accent"])], background=[("active", th["panel"])])


def entry(master, theme, fonts, width=30, show=None, textvariable=None):
    e = tk.Entry(master, bg=theme["input"], fg=theme["text"], insertbackground=theme["accent"], relief="flat",
                 font=fonts["body"], highlightthickness=1, highlightbackground=theme["line_2"],
                 highlightcolor=theme["accent"], width=width, show=show, textvariable=textvariable)
    return e


def textbox(master, theme, fonts, height=3):
    return tk.Text(master, bg=theme["input"], fg=theme["text"], insertbackground=theme["accent"], relief="flat",
                   font=fonts["body"], highlightthickness=1, highlightbackground=theme["line_2"],
                   highlightcolor=theme["accent"], height=height, wrap="word", padx=10, pady=8, undo=True)
