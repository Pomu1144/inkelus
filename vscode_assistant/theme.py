"""Colours and fonts for the three interface styles.

pro    neutral dark editor greys, a blue accent, sans type, flat controls (default)
ink    charcoal ink, cream paper and gold, serif type, thin square frames
glass  dark slate tiles, white hairlines, condensed titles, a yellow accent

Ink and Glass are ported from NXBNVNB.

Tk has no translucency, so the "glass" alpha values from the CSS are baked
into solid colours over the dark backdrop.
"""
import ctypes
import ctypes.util
import os
import sys

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

INK = {
    "name": "ink",
    "bg": "#0a0a0b",          # page backdrop
    "rail": "#08080a",
    "panel": "#121214",       # --g-glass
    "panel_2": "#0c0c0e",     # --g-glass-2
    "panel_hi": "#24201a",    # --g-glass-hi (user turns)
    "input": "#0f0f11",
    "line": "#3a352a",        # --g-line (gold hairline at 20%)
    "line_2": "#6b5f45",      # --g-line-2
    "text": "#f1ebdc",
    "dim": "#b3ada0",
    "faint": "#7c776c",
    "accent": "#c9a24e",
    "accent_2": "#e8cf8c",
    "on_accent": "#17130b",
    "sel": "#ece2c6",
    "on_sel": "#17130b",
    "slip_top": "#f6eed8",    # the cream paper slip (main action)
    "slip_bot": "#e3d4ad",
    "red": "#d9604f",
    "green": "#a8c98a",
    "code_bg": "#070707",
    "wash": "#3b311d",        # active nav row: gold wash start
    "radius_square": True,
    "upper_titles": False,
    "ornate": True,           # ink art, spaced titles, Japanese captions
    "ticks": True,            # short accent tick on panel tops
}

GLASS = {
    "name": "glass",
    "bg": "#07090b",
    "rail": "#0b0e12",
    "panel": "#141a20",
    "panel_2": "#0d1115",
    "panel_hi": "#1f272f",
    "input": "#0d1115",
    "line": "#2a3036",
    "line_2": "#4a5157",
    "text": "#f3f2ee",
    "dim": "#b1b0ad",
    "faint": "#7b7b79",
    "accent": "#d4a84a",
    "accent_2": "#ebc976",
    "on_accent": "#1a1408",
    "sel": "#f4f4f1",
    "on_sel": "#14181c",
    "slip_top": "#d4a84a",
    "slip_bot": "#d4a84a",
    "red": "#ff6a5a",
    "green": "#7fd18b",
    "code_bg": "#05070a",
    "wash": "#f4f4f1",
    "radius_square": False,
    "upper_titles": True,
    "ornate": False,
    "ticks": True,
}

PRO = {
    "name": "pro",
    "bg": "#1e1e1e",
    "rail": "#181818",
    "panel": "#252526",
    "panel_2": "#202020",
    "panel_hi": "#2b2d31",    # user turns
    "input": "#2a2a2a",
    "line": "#2e2e2e",
    "line_2": "#3e3e42",
    "text": "#e4e4e4",
    "dim": "#a9a9a9",
    "faint": "#767676",
    "accent": "#0078d4",
    "accent_2": "#4daafc",
    "on_accent": "#ffffff",
    "sel": "#04395e",
    "on_sel": "#ffffff",
    "slip_top": "#0078d4",
    "slip_bot": "#0078d4",
    "red": "#f14c4c",
    "green": "#3fb950",
    "code_bg": "#161616",
    "wash": "#37373d",        # active list row
    "radius_square": True,
    "upper_titles": False,
    "ornate": False,
    "ticks": False,
}

STYLES = {"pro": PRO, "ink": INK, "glass": GLASS}
DEFAULT_STYLE = "pro"


def _register_fonts():
    """Make the bundled TTFs visible to Tk for this process only, without
    installing anything system-wide (works without admin rights)."""
    files = [os.path.join(ASSETS, f) for f in os.listdir(ASSETS) if f.endswith(".ttf")]
    try:
        if sys.platform == "win32":
            FR_PRIVATE = 0x10
            add = ctypes.windll.gdi32.AddFontResourceExW
            for f in files:
                add(f, FR_PRIVATE, 0)
        elif sys.platform == "darwin":
            ct = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreText"))
            cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
            cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
            cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
            ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            for f in files:
                b = f.encode()
                url = cf.CFURLCreateFromFileSystemRepresentation(None, b, len(b), False)
                ct.CTFontManagerRegisterFontsForURL(url, 1, None)  # kCTFontManagerScopeProcess
        else:
            lib = ctypes.util.find_library("fontconfig")
            if lib:
                fc = ctypes.cdll.LoadLibrary(lib)
                fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                for f in files:
                    fc.FcConfigAppFontAddFile(None, f.encode())
    except Exception:
        pass  # fall back to system serif / sans fonts


_registered = False


def register_fonts():
    global _registered
    if not _registered:
        _register_fonts()
        _registered = True


def pick(families, available):
    low = {f.lower(): f for f in available}
    for f in families:
        if f.lower() in low:
            return low[f.lower()]
    return families[-1]


def fonts_for(style, available):
    """Return font tuples keyed by role for the given style."""
    # The bundled Kaisei files register as two families: "Kaisei Tokumin
    # Medium" (regular text) and "Kaisei Tokumin" (the bold cut).
    serif = pick(["Kaisei Tokumin Medium", "Hiragino Mincho ProN", "Yu Mincho", "Noto Serif JP", "Georgia",
                  "Times New Roman", "serif"], available)
    serif_b = pick(["Kaisei Tokumin", serif], available)
    cinzel = pick(["Cinzel", "Trajan Pro", serif], available)
    sans = pick(["Inter", "Segoe UI", "Helvetica Neue", "Arial", "sans-serif"], available)
    cond = pick(["Oswald", "Bahnschrift", "Arial Narrow", sans], available)
    mono = pick(["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Courier New", "monospace"], available)
    if style == "ink":
        return {
            "body": (serif, 11), "small": (serif, 9), "label": (serif, 10),
            "title": (serif_b, 19, "bold"), "h2": (serif_b, 13, "bold"),
            "sub": (cinzel, 7), "brand": (cinzel, 8), "slip": (cinzel, 15, "bold italic"),
            "nav": (serif, 11), "btn": (serif, 10), "mono": (mono, 10),
            "md_h1": (serif_b, 15, "bold"), "md_h2": (serif_b, 13, "bold"), "md_h3": (serif_b, 12, "bold"),
            "bold_family": serif_b,
        }
    if style == "pro":
        return {
            "body": (sans, 10), "small": (sans, 8), "label": (sans, 9),
            "title": (sans, 14, "bold"), "h2": (sans, 11, "bold"), "sub": (sans, 8), "brand": (sans, 10, "bold"),
            "slip": (sans, 10, "bold"), "nav": (sans, 10), "btn": (sans, 9), "mono": (mono, 10),
            "md_h1": (sans, 14, "bold"), "md_h2": (sans, 12, "bold"), "md_h3": (sans, 11, "bold"),
            "bold_family": sans,
        }
    return {
        "body": (sans, 10), "small": (sans, 8), "label": (sans, 9),
        "title": (cond, 19), "h2": (cond, 13), "sub": (cond, 8), "brand": (cond, 9),
        "slip": (cond, 13, "bold"), "nav": (cond, 11), "btn": (cond, 10), "mono": (mono, 10),
        "md_h1": (sans, 14, "bold"), "md_h2": (sans, 12, "bold"), "md_h3": (sans, 11, "bold"),
        "bold_family": sans,
    }


def case(theme, s):
    """Glass sets labels in capitals; the other styles keep them as written."""
    return s.upper() if theme["upper_titles"] else s


def mix(c1, c2, t):
    """Blend two #rrggbb colours (t=0 → c1, t=1 → c2)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))
