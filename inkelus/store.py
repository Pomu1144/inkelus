"""Settings and conversations, saved as JSON in your user folder.

Windows: %APPDATA%\\Inkelus    macOS: ~/Library/Application Support/Inkelus
Linux:   ~/.config/inkelus    (override with INKELUS_HOME)
"""
import json
import os
import sys
import tempfile
import time
import uuid


def data_dir():
    if os.environ.get("INKELUS_HOME"):
        d = os.environ["INKELUS_HOME"]
    elif sys.platform == "win32":
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Inkelus")
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/Inkelus")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "inkelus")
    os.makedirs(d, exist_ok=True)
    return d


DEFAULT_SETTINGS = {
    "style": "ink",
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
}


def new_id():
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self):
        self.dir = data_dir()
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.chats_path = os.path.join(self.dir, "chats.json")
        self.settings = self._merge(DEFAULT_SETTINGS, self._load(self.settings_path, {}))
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
            "params": {"system": self.settings["defaults"].get("system", ""), "temperature": 0.7,
                       "top_p": 0.9, "num_ctx": None, "max_tokens": None},
            "messages": [], "updated": time.time(),
        }
        self.chats.insert(0, c)
        return c
