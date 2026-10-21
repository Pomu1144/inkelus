"""Work context: pull requests (GitHub), issues (Jira) and email (IMAP),
linked into one graph the model can read.

Everything is read-only and standard library only:

* GitHub   GET /repos/{owner}/{repo}/pulls        (token optional for public repos)
* Jira     GET /rest/api/{2|3}/issue/{key} and an optional JQL search
* Email    IMAP over TLS, the folder is opened read-only (EXAMINE) and
           messages are fetched with BODY.PEEK, so nothing is marked as read

Items are linked when one mentions another: a Jira key (ABC-123) in a PR's
title, branch or description, or a PR link / "owner/repo#12" / Jira key in
an email. Linked items form a "thread", which is what the diagram draws and
what the model gets as context.

A sync writes the result to context.json in the app's data folder, so the graph
is there offline and on the next start. Credentials live in settings.json under
"context" (masked whenever the page reads the settings).
"""
import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import re
import ssl
import urllib.parse
from base64 import b64encode
from datetime import datetime, timedelta, timezone

from . import net

TEXT_LIMIT = 4000
JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
# things that look like Jira keys but never are
NOT_JIRA = {"UTF", "SHA", "ISO", "RFC", "CVE", "COVID", "MD", "HTTP", "TLS", "SSL", "AES", "RSA", "GPT", "WCAG",
            "PEP", "ES", "EN", "IEC", "IEEE", "UTC", "GMT", "CET", "PST", "EST", "PDT", "EDT", "BST", "IPV"}
PR_URL = re.compile(r"/([\w.-]+)/([\w.-]+)/pull/(\d+)")
PR_REF = re.compile(r"\b([\w.-]+)/([\w.-]+)#(\d+)\b")
PR_WORD = re.compile(r"\b(?:PR|pull request|pull)\s*#(\d+)\b", re.I)

DEFAULTS = {
    "gh_api": "https://api.github.com",
    "gh_token": "",
    "gh_repos": "",            # owner/repo, comma or newline separated
    "gh_limit": 30,            # most recently updated PRs per repo
    "jira_url": "",            # https://yourcompany.atlassian.net
    "jira_email": "",          # Jira Cloud: your login email (empty = Server/DC personal access token)
    "jira_token": "",
    "jira_projects": "",       # optional: only these project keys count as Jira keys
    "jira_jql": "",            # optional extra issues, e.g. assignee = currentUser() AND updated >= -14d
    "mail_host": "",           # imap.gmail.com, outlook.office365.com…
    "mail_port": 993,
    "mail_user": "",
    "mail_password": "",       # an app password, not your normal one
    "mail_folder": "INBOX",
    "mail_days": 14,
    "mail_limit": 150,
}

TOOLS = [
    {"type": "function", "function": {
        "name": "context_overview",
        "description": "List the threads of linked work items (GitHub pull requests, Jira issues and emails that "
                       "mention each other), most recently active first, with their ids.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "context_search",
        "description": "Search pull requests, Jira issues and emails by words in their title, text, author or "
                       "status. Returns matching items with their ids.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for, e.g. 'login timeout' or 'ABC-123'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "context_item",
        "description": "Read one item in full (description / body, status, people, dates) plus a summary of "
                       "every item linked to it.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "An id from context_overview or context_search, a Jira "
                                                    "key like ABC-123, or a PR like owner/repo#12"}},
            "required": ["id"]}}},
]
TOOL_NAMES = [t["function"]["name"] for t in TOOLS]
SECRETS = ("gh_token", "jira_token", "mail_password")

SYSTEM_PROMPT = (
    "You can read the user's work context: GitHub pull requests, Jira issues and emails, linked where they "
    "mention each other. Start with context_overview, use context_search to find items and context_item to "
    "read one with everything linked to it. Connect the dots across sources (what the issue asks for, what "
    "the PR changes, what people said by email) and cite item ids like ABC-123 or owner/repo#12."
)

KINDS = (("jira", "Jira"), ("pr", "Pull requests"), ("mail", "Email"))


def _lines(s):
    return [x.strip() for x in re.split(r"[,\n]", s or "") if x.strip()]


def _clip(s, n=TEXT_LIMIT):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


# ── GitHub ─────────────────────────────────────────────────────────────────
def fetch_github(cfg):
    api = (cfg.get("gh_api") or DEFAULTS["gh_api"]).rstrip("/")
    hdrs = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "VSCodeAssistant"}
    if cfg.get("gh_token"):
        hdrs["Authorization"] = "Bearer " + cfg["gh_token"].strip()
    limit = max(1, min(int(cfg.get("gh_limit") or 30), 100))
    nodes, errors = [], []
    for repo in _lines(cfg.get("gh_repos")):
        repo = repo.strip("/")
        if repo.count("/") != 1:
            errors.append("GitHub: %r should look like owner/repo." % repo)
            continue
        url = "%s/repos/%s/pulls?state=all&sort=updated&direction=desc&per_page=%d" % (api, repo, limit)
        try:
            prs = net.get_json(url, headers=hdrs)
        except Exception as e:
            errors.append("GitHub %s: %s" % (repo, e))
            continue
        for p in prs if isinstance(prs, list) else []:
            state = "merged" if p.get("merged_at") else ("draft" if p.get("draft") and p.get("state") == "open"
                                                         else p.get("state", ""))
            nodes.append({
                "id": "%s#%d" % (repo, p["number"]), "kind": "pr",
                "title": p.get("title") or "", "state": state,
                "who": (p.get("user") or {}).get("login", ""),
                "url": p.get("html_url", ""), "updated": p.get("updated_at") or "",
                "fields": {"branch": "%s → %s" % ((p.get("head") or {}).get("ref", "?"),
                                                  (p.get("base") or {}).get("ref", "?")),
                           "created": p.get("created_at") or "",
                           "reviewers": ", ".join(r.get("login", "") for r in p.get("requested_reviewers") or [])},
                "text": _clip(p.get("body") or ""),
                "scan": " ".join((p.get("title") or "", (p.get("head") or {}).get("ref", ""), p.get("body") or "")),
            })
    return nodes, errors


# ── Jira ───────────────────────────────────────────────────────────────────
def _adf_text(node):
    """Flatten Jira Cloud's Atlassian Document Format to plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t in ("mention", "emoji", "inlineCard"):
        a = node.get("attrs") or {}
        return a.get("text") or a.get("shortName") or a.get("url") or ""
    inner = _adf_text(node.get("content") or [])
    return inner + "\n" if t in ("paragraph", "heading", "listItem", "codeBlock", "blockquote", "rule") else inner


def _jira_client(cfg):
    base = (cfg.get("jira_url") or "").rstrip("/")
    token = (cfg.get("jira_token") or "").strip()
    hdrs = {"Accept": "application/json", "User-Agent": "VSCodeAssistant"}
    if cfg.get("jira_email"):  # Jira Cloud: basic auth with email + API token, REST v3
        hdrs["Authorization"] = "Basic " + b64encode(("%s:%s" % (cfg["jira_email"].strip(), token)).encode()).decode()
        ver = "3"
    else:                      # Server / Data Center: personal access token, REST v2
        if token:
            hdrs["Authorization"] = "Bearer " + token
        ver = "2"
    return base, ver, hdrs


FIELDS = "summary,status,assignee,reporter,issuetype,priority,updated,created,description"


def _jira_node(base, issue):
    f = issue.get("fields") or {}
    desc = f.get("description")
    return {
        "id": issue["key"], "kind": "jira",
        "title": f.get("summary") or "", "state": ((f.get("status") or {}).get("name") or "").lower(),
        "who": (f.get("assignee") or {}).get("displayName", "") or "unassigned",
        "url": "%s/browse/%s" % (base, issue["key"]), "updated": _jira_time(f.get("updated")),
        "fields": {"type": (f.get("issuetype") or {}).get("name", ""),
                   "priority": (f.get("priority") or {}).get("name", ""),
                   "reporter": (f.get("reporter") or {}).get("displayName", ""),
                   "created": _jira_time(f.get("created"))},
        "text": _clip(_adf_text(desc) if not isinstance(desc, str) else desc),
        "scan": " ".join((f.get("summary") or "", desc if isinstance(desc, str) else _adf_text(desc))),
    }


def _jira_time(s):
    if not s:
        return ""
    try:  # 2024-05-01T10:20:30.000+0000
        return _iso(datetime.strptime(s[:19] + s[23:].replace(":", ""), "%Y-%m-%dT%H:%M:%S%z"))
    except ValueError:
        return s


def fetch_jira(cfg, keys):
    base, ver, hdrs = _jira_client(cfg)
    if not base:
        return [], []
    nodes, errors, seen = [], [], set()
    jql = (cfg.get("jira_jql") or "").strip()
    if jql:
        if ver == "3":  # Cloud retired /search in 2025 in favour of /search/jql
            url = "%s/rest/api/3/search/jql?%s" % (base, urllib.parse.urlencode(
                {"jql": jql, "fields": FIELDS, "maxResults": 50}))
        else:
            url = "%s/rest/api/2/search?%s" % (base, urllib.parse.urlencode(
                {"jql": jql, "fields": FIELDS, "maxResults": 50}))
        try:
            for issue in net.get_json(url, headers=hdrs).get("issues") or []:
                nodes.append(_jira_node(base, issue))
                seen.add(issue["key"])
        except Exception as e:
            errors.append("Jira search: %s" % e)
    failures, missing = 0, 0
    wanted = sorted(keys - seen)[:80]
    for key in wanted:
        url = "%s/rest/api/%s/issue/%s?fields=%s" % (base, ver, key, FIELDS)
        try:
            nodes.append(_jira_node(base, net.get_json(url, headers=hdrs)))
        except net.HTTPError as e:
            if e.status in (401, 403):
                errors.append("Jira: %s (check the email and API token)." % e)
                break
            missing += 1    # 404: a key-shaped string that isn't one of your issues
        except Exception as e:
            failures += 1
            if failures >= 3:
                errors.append("Jira: %s" % e)
                break
    if wanted and missing == len(wanted) and not nodes:
        errors.append("Jira: none of the %d issue keys found in pull requests and email (%s…) exist on %s. Check the "
                      "site URL, and leave the email empty only for Jira Server / Data Center." % (len(wanted), ", ".join(wanted[:3]), base))
    return nodes, errors


# ── Email ──────────────────────────────────────────────────────────────────
def _decode(h):
    try:
        return str(email.header.make_header(email.header.decode_header(h or "")))
    except Exception:
        return h or ""


def _body(msg):
    plain, html_part = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    if plain is None and html_part:
        plain = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_part)
        plain = re.sub(r"<[^>]+>", " ", plain)
        plain = html.unescape(plain)
    text = plain or ""
    # drop quoted replies so a thread doesn't repeat itself
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">"))
    text = re.split(r"\n\s*On .{5,200}wrote:\s*\n", text)[0]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_email(cfg):
    host = (cfg.get("mail_host") or "").strip()
    if not host:
        return [], []
    days = max(1, min(int(cfg.get("mail_days") or 14), 365))
    limit = max(1, min(int(cfg.get("mail_limit") or 150), 1000))
    folder = cfg.get("mail_folder") or "INBOX"
    nodes = []
    try:
        box = imaplib.IMAP4_SSL(host, int(cfg.get("mail_port") or 993), ssl_context=ssl.create_default_context())
        box.sock.settimeout(30)
    except Exception as e:
        return [], ["Email: can't connect to %s (%s)" % (host, e)]
    try:
        try:
            box.login(cfg.get("mail_user") or "", cfg.get("mail_password") or "")
        except imaplib.IMAP4.error as e:
            return [], ["Email: login failed (%s). Most providers need an app password here." %
                        (e.args[0].decode() if e.args and isinstance(e.args[0], bytes) else e)]
        typ, _ = box.select('"%s"' % folder.replace('"', r'\"'), readonly=True)
        if typ != "OK":
            return [], ["Email: can't open folder %r." % folder]
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = box.search(None, "SINCE", since)
        ids = (data[0] or b"").split()[-limit:] if typ == "OK" else []
        for i in reversed(ids):
            typ, parts = box.fetch(i, "(BODY.PEEK[]<0.80000>)")  # first 80 KB is plenty for the text
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ != "OK" or not raw:
                continue
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject")) or "(no subject)"
            who = email.utils.parseaddr(_decode(msg.get("From")))
            try:
                when = _iso(email.utils.parsedate_to_datetime(msg.get("Date")))
            except Exception:
                when = ""
            mid = (msg.get("Message-ID") or "").strip("<> ") or "%s-%s" % (folder, i.decode())
            body = _body(msg)
            nodes.append({
                "id": "mail:" + mid, "kind": "mail",
                "title": subject, "state": "", "who": who[0] or who[1],
                "url": "", "updated": when,
                "fields": {"from": who[1], "to": _decode(msg.get("To"))[:200], "folder": folder},
                "text": _clip(body),
                "scan": subject + " " + body,
            })
    except Exception as e:
        return nodes, ["Email: %s" % e]
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return nodes, []


# ── Linking ────────────────────────────────────────────────────────────────
def jira_keys(text, projects=None):
    out = set()
    for proj, num in JIRA_KEY.findall(text or ""):
        if proj in NOT_JIRA or (projects and proj not in projects):
            continue
        out.add("%s-%s" % (proj, num))
    return out


def pr_refs(text, repos):
    """PR ids (owner/repo#N) mentioned in text, limited to the synced repos."""
    by_lower = {r.lower(): r for r in repos}
    by_name = {}
    for r in repos:
        by_name.setdefault(r.split("/")[1].lower(), []).append(r)
    out = set()
    for owner, name, num in PR_URL.findall(text or "") + PR_REF.findall(text or ""):
        r = by_lower.get(("%s/%s" % (owner, name)).lower())
        if r:
            out.add("%s#%s" % (r, num))
    for m in re.finditer(r"\b([\w.-]+)#(\d+)\b", text or ""):  # "repo#12"
        for r in by_name.get(m.group(1).lower(), []):
            out.add("%s#%s" % (r, m.group(2)))
    if len(repos) == 1:  # "PR #12" is only unambiguous with one repo
        out.update("%s#%s" % (repos[0], n) for n in PR_WORD.findall(text or ""))
    return out


def link(nodes, repos, projects=None):
    ids = {n["id"] for n in nodes}
    edges = set()
    for n in nodes:
        refs = jira_keys(n.get("scan", ""), projects) | pr_refs(n.get("scan", ""), repos)
        for r in refs:
            if r in ids and r != n["id"]:
                edges.add(tuple(sorted((n["id"], r))))
    return sorted(edges)


def sync(cfg):
    """Fetch every configured source (on a worker thread) and return a Graph."""
    repos = [r.strip("/") for r in _lines(cfg.get("gh_repos")) if r.strip("/").count("/") == 1]
    projects = {p.upper() for p in _lines(cfg.get("jira_projects"))} or None
    errors = []
    prs, err = fetch_github(cfg)
    errors += err
    mails, err = fetch_email(cfg)
    errors += err
    keys = set()
    for n in prs + mails:
        keys |= jira_keys(n["scan"], projects)
    issues, err = fetch_jira(cfg, keys)
    errors += err
    nodes = prs + issues + mails
    edges = link(nodes, repos, projects)
    for n in nodes:
        n.pop("scan", None)
    return Graph(nodes, edges, synced=_iso(datetime.now(timezone.utc)), errors=errors)


def update_settings(cfg, new, mask):
    """Apply settings from the page. A secret sent back as the mask keeps its saved value; an empty one clears it."""
    for k, default in DEFAULTS.items():
        if k not in new:
            continue
        v = new[k]
        if k in SECRETS:
            if v != mask:
                cfg[k] = str(v or "").strip()
            continue
        if isinstance(default, int):
            try:
                cfg[k] = max(1, int(v))
            except (TypeError, ValueError):
                pass
        else:
            cfg[k] = str(v or "").strip()
    return cfg


def masked(cfg, mask):
    out = dict(DEFAULTS, **(cfg or {}))
    for k in SECRETS:
        out[k] = mask if out.get(k) else ""
    return out


def configured(cfg):
    return [label for key, label in (("gh_repos", "GitHub"), ("jira_url", "Jira"), ("mail_host", "Email"))
            if (cfg.get(key) or "").strip()]


# ── The graph ──────────────────────────────────────────────────────────────
class Graph:
    def __init__(self, nodes=(), edges=(), synced="", errors=()):
        self.nodes = {n["id"]: n for n in nodes}
        self.edges = [tuple(e) for e in edges if e[0] in self.nodes and e[1] in self.nodes]
        self.synced = synced
        self.errors = list(errors)
        self.adj = {i: set() for i in self.nodes}
        for a, b in self.edges:
            self.adj[a].add(b)
            self.adj[b].add(a)

    # persistence
    def to_json(self):
        return {"synced": self.synced, "errors": self.errors, "nodes": list(self.nodes.values()),
                "edges": [list(e) for e in self.edges]}

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return cls(d.get("nodes") or [], d.get("edges") or [], d.get("synced", ""), d.get("errors") or [])
        except (OSError, ValueError, KeyError, TypeError):
            return cls()

    def for_page(self):
        """What the Context page draws: every item, the links, and the threads (lists of ids)."""
        return dict(self.to_json(), threads=self.threads(),
                    loose=sorted((i for i in self.nodes if not self.adj[i]),
                                 key=lambda i: self.nodes[i].get("updated") or "", reverse=True))

    # structure
    def threads(self):
        """Connected groups of 2+ items, most recently active first."""
        seen, out = set(), []
        for start in self.nodes:
            if start in seen or not self.adj[start]:
                continue
            group, stack = [], [start]
            seen.add(start)
            while stack:
                cur = stack.pop()
                group.append(cur)
                for nb in self.adj[cur]:
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            out.append(sorted(group, key=lambda i: (self.nodes[i]["kind"], i)))
        out.sort(key=lambda g: max(self.nodes[i].get("updated") or "" for i in g), reverse=True)
        return out

    def resolve(self, ref):
        ref = (ref or "").strip()
        if ref in self.nodes:
            return ref
        low = ref.lower()
        for i in self.nodes:
            if i.lower() == low or i.lower() == "mail:" + low:
                return i
        if re.fullmatch(r"#?\d+", ref):  # "#12" when only one repo has that PR
            hits = [i for i in self.nodes if self.nodes[i]["kind"] == "pr" and i.endswith("#" + ref.lstrip("#"))]
            if len(hits) == 1:
                return hits[0]
        return None

    # text for the model
    def line(self, i):
        n = self.nodes[i]
        bits = [n["kind"].upper(), i, "“%s”" % n["title"]]
        if n.get("state"):
            bits.append("[%s]" % n["state"])
        if n.get("who"):
            bits.append("by %s" % n["who"] if n["kind"] != "jira" else "assignee %s" % n["who"])
        if n.get("updated"):
            bits.append(n["updated"][:10])
        return " ".join(bits)

    def overview(self):
        if not self.nodes:
            return "No work context synced yet. The user can add GitHub, Jira and email in Settings › Work " \
                   "context and press Sync on the Context page."
        counts = {k: sum(1 for n in self.nodes.values() if n["kind"] == k) for k, _l in KINDS}
        out = ["Synced %s: %d pull requests, %d Jira issues, %d emails." % (
            self.synced[:16].replace("T", " ") or "?", counts["pr"], counts["jira"], counts["mail"]), ""]
        threads = self.threads()
        for t_i, group in enumerate(threads[:25], 1):
            out.append("Thread %d:" % t_i)
            out += ["  - " + self.line(i) for i in group[:12]]
            if len(group) > 12:
                out.append("  … and %d more" % (len(group) - 12))
        if len(threads) > 25:
            out.append("… and %d more threads (use context_search)." % (len(threads) - 25))
        loose = [i for i in self.nodes if not self.adj[i] and self.nodes[i]["kind"] != "mail"]
        if loose:
            loose.sort(key=lambda i: self.nodes[i].get("updated") or "", reverse=True)
            out += ["", "Not linked to anything (most recent):"] + ["  - " + self.line(i) for i in loose[:15]]
        unlinked_mail = sum(1 for i in self.nodes if not self.adj[i] and self.nodes[i]["kind"] == "mail")
        if unlinked_mail:
            out.append("  (+ %d emails that don't mention a PR or issue; find them with context_search)" % unlinked_mail)
        return "\n".join(out)

    def search(self, query, limit=15):
        words = [w for w in (query or "").lower().split() if w]
        if not words:
            raise RuntimeError("context_search needs some words to look for.")
        hits = []
        for i, n in self.nodes.items():
            hay = " ".join((i, n["title"], n.get("text", ""), n.get("who", ""), n.get("state", ""),
                            " ".join(str(v) for v in (n.get("fields") or {}).values()))).lower()
            if all(w in hay for w in words):
                score = sum(3 for w in words if w in (i + " " + n["title"]).lower())
                hits.append((score, n.get("updated") or "", i))
        if not hits:
            return "Nothing matches %r." % query
        hits.sort(reverse=True)
        return "\n".join(["%d match(es) for %r:" % (len(hits), query)] +
                         ["- " + self.line(i) for _s, _u, i in hits[:limit]] +
                         (["… %d more" % (len(hits) - limit)] if len(hits) > limit else []))

    def detail(self, ref):
        i = self.resolve(ref)
        if not i:
            raise RuntimeError("No item %r. Use context_search or context_overview to find ids." % ref)
        n = self.nodes[i]
        out = [self.line(i)]
        if n.get("url"):
            out.append("URL: " + n["url"])
        for k, v in (n.get("fields") or {}).items():
            if v:
                out.append("%s: %s" % (k.capitalize(), v))
        out += ["", n.get("text") or "(no description)"]
        if self.adj[i]:
            out += ["", "Linked items:"]
            for j in sorted(self.adj[i]):
                m = self.nodes[j]
                snippet = " ".join((m.get("text") or "").split())[:300]
                out.append("- " + self.line(j) + ("\n    " + snippet if snippet else ""))
            # the rest of the thread (linked through those), so one call answers "what did people say about the fix?"
            group = next((g for g in self.threads() if i in g), [])
            rest = [j for j in group if j != i and j not in self.adj[i]]
            if rest:
                out += ["", "Also in this thread (linked through the items above):"]
                for j in rest[:10]:
                    snippet = " ".join((self.nodes[j].get("text") or "").split())[:200]
                    out.append("- " + self.line(j) + ("\n    " + snippet if snippet else ""))
        return "\n".join(out)

    def run(self, name, args):
        args = args or {}
        if name == "context_overview":
            return self.overview()
        if name == "context_search":
            return self.search(str(args.get("query", "")))
        if name == "context_item":
            return self.detail(str(args.get("id", "")))
        raise RuntimeError("Unknown tool %s" % name)
