"""Settings and conversations, saved as JSON in your user folder.

Windows: %APPDATA%\\VSCode Assistant
macOS:   ~/Library/Application Support/VSCode Assistant
Linux:   ~/.config/vscode-assistant
Override with VSCODE_ASSISTANT_HOME. An existing folder from when the app was
called Inkelus is still used, so nothing is lost after the rename.
"""
import json
import os
import sys
import tempfile
import time
import uuid


def _platform_dir(name, linux_name):
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), name)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/" + name)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), linux_name)


def data_dir():
    d = os.environ.get("VSCODE_ASSISTANT_HOME") or os.environ.get("INKELUS_HOME")
    if not d:
        d = _platform_dir("VSCode Assistant", "vscode-assistant")
        legacy = _platform_dir("Inkelus", "inkelus")
        if not os.path.isdir(d) and os.path.isdir(legacy):
            d = legacy
    os.makedirs(d, exist_ok=True)
    return d


DEFAULT_SETTINGS = {
    "connections": [{"id": "ollama", "name": "Ollama", "type": "ollama", "url": "http://localhost:11434", "key": ""}],
    "default_conn": "ollama",
    "last_model": "",
    "defaults": {"system": "", "keep_alive": ""},
    "web": {                      # the built-in sandbox browser
        "confirm": True,          # ask before each page / search
        "allow_search": True,
        "allow_private": False,   # refuse localhost / intranet addresses
        "max_steps": 8,
    },
    "groups": [],                 # chat groups in the sidebar: [{"id", "name", "collapsed"}]
    "agent": {                    # the agent browser (a real Chrome window the model can drive)
        "confirm": "actions",     # "actions": ask before clicks/typing/opening; "all"; "none"
        "chrome_path": "",        # empty: find Chrome / Edge / Chromium automatically
        "max_steps": 25,
    },
}


def new_id():
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self):
        self.dir = data_dir()
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.chats_path = os.path.join(self.dir, "chats.json")
        saved = self._load(self.settings_path, {})
        self.settings = self._merge(DEFAULT_SETTINGS, saved)
        self.chats = self._load(self.chats_path, [])
        if not isinstance(self.chats, list):
            self.chats = []

    @staticmethod
    def _merge(defaults, saved):
        out = json.loads(json.dumps(defaults))
        for k, v in (saved or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            else:
                out[k] = v
        return out

    @staticmethod
    def _load(path, fallback):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return fallback

    @staticmethod
    def _save(path, data):
        # write-then-rename so a crash never leaves a half-written file
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def save_settings(self):
        self._save(self.settings_path, self.settings)

    def save_chats(self):
        self._save(self.chats_path, self.chats)

    def conn(self, cid):
        return next((c for c in self.settings["connections"] if c["id"] == cid), None)

    def new_chat(self, model=""):
        c = {
            "id": new_id(), "title": "New conversation", "titled": False,
            "model": model or self.settings.get("last_model", ""),
            "browser": False,
            "agent": False,           # may drive the agent browser window
            "docs": [],               # Library documents shared with the model
            "template": "auto",       # "auto", "" (none) or a template name
            "params": {"system": self.settings["defaults"].get("system", ""), "temperature": 0.7,
                       "top_p": 0.9, "num_ctx": None, "max_tokens": None},
            "messages": [], "updated": time.time(),
        }
        self.chats.insert(0, c)
        return c
