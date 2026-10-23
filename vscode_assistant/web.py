"""VSCode Assistant's built-in sandbox browser: read-only web tools for the model.

This is NOT BrowserOS and never touches any browser on your computer. It is
a tiny text-only fetcher written on the standard library:

* GET requests only: it can't click, submit forms, log in or post anything
* no cookies, no saved logins, no JavaScript, no downloads to disk
* only http(s), only text / HTML / JSON / XML, at most 3 MB per page
* local and private-network addresses (localhost, 10.x, 192.168.x, your
  intranet…) are refused unless you allow them in Settings, including when
  a page tries to redirect there

Each chat gets its own Sandbox, so pages opened in one chat aren't visible to
another.
"""
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import docread

MAX_BYTES = 3 * 1024 * 1024
PAGE_CHARS = 8000
MAX_LINKS = 40
UA = "Mozilla/5.0 (compatible; VSCodeAssistant/2.0; text-only reader)"
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml", "application/rss",
              "application/atom", "application/ld+json")

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (DuckDuckGo). Returns numbered results with title, URL and snippet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search for"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_page",
        "description": "Open a web page and return its title, readable text (first part) and a numbered list "
                       "of links. Read-only.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Full http(s) URL"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "follow_link",
        "description": "Open link number N from the page opened last.",
        "parameters": {"type": "object", "properties": {
            "number": {"type": "integer", "description": "Link number from the last page's link list"}},
            "required": ["number"]}}},
    {"type": "function", "function": {
        "name": "read_more",
        "description": "Continue reading the page opened last, from where the previous text stopped.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "find_in_page",
        "description": "Find a word or phrase in the page opened last and return the surrounding text.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Word or phrase to look for"}}, "required": ["text"]}}},
]
TOOL_NAMES = [t["function"]["name"] for t in TOOLS]

SYSTEM_PROMPT = (
    "You have a sandboxed, read-only web browser (VSCode Assistant web tools). Use web_search to find pages, "
    "open_page or follow_link to read them, read_more for long pages and find_in_page to locate details. "
    "It shows page text only: no JavaScript, logins or forms. Base answers on what you read, and cite "
    "the URLs you used."
)


class BlockedURL(Exception):
    pass


def _private(host):
    """True if host is, or resolves to, a loopback / private / link-local address."""
    h = (host or "").strip("[]").lower()
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan", ".corp", ".home")):
        return True
    try:
        addrs = [ipaddress.ip_address(h)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(h, None)]
        except (socket.gaierror, ValueError, UnicodeError):
            return False  # unknown name: let the request fail normally
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast
               or a.is_unspecified or (a.version == 4 and a in cgnat) for a in addrs)


class _Guard(urllib.request.HTTPRedirectHandler):
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.sandbox.check(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            for h in ("Cookie", "Authorization"):
                new.remove_header(h)
        return new


class _NoCookies(urllib.request.BaseHandler):
    """Strip any cookie the environment might add; never store Set-Cookie."""
    handler_order = 999

    def http_request(self, req):
        req.remove_header("Cookie")
        return req

    https_request = http_request


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head", "nav", "footer", "form",
            "button", "select"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "main", "aside",
             "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dt", "dd", "figcaption", "hr"}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out, self.links, self.title = [], [], ""
        self.skip = 0
        self.in_title = False
        self.link = None
        self.main_depth = 0  # inside <main> / <article>: preferred over menus and sidebars
        self.main_out = []

    def _emit(self, piece):
        self.out.append(piece)
        if self.main_depth:
            self.main_out.append(piece)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in ("main", "article") or dict(attrs).get("role") == "main":
            self.main_depth += 1
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag in self.BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3"):
            self._emit("## ")
        if tag == "li":
            self._emit("• ")
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                self.link = [urllib.parse.urljoin(self.base, href), []]

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in ("main", "article") and self.main_depth:
            self.main_depth -= 1
        if tag in self.SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self._emit("\n")
        if tag == "a" and self.link:
            text = " ".join("".join(self.link[1]).split())
            url = self.link[0].split("#")[0]
            if text and url.startswith(("http://", "https://")) and all(u != url for u, _t in self.links):
                self.links.append((url, text[:80]))
                self._emit(" [%d]" % len(self.links))
            self.link = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self._emit(data)
        if self.link is not None:
            self.link[1].append(data)

    def text(self):
        main = "".join(self.main_out)
        t = main if len(main.strip()) > 400 else "".join(self.out)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r" *\n *", "\n", t)
        # drop lines that are only bullets / heading marks / link numbers (menus)
        t = "\n".join(ln for ln in t.split("\n") if not re.fullmatch(r"[•#\s]*(\[\d+\]\s*)?", ln))
        return re.sub(r"\n{2,}", "\n\n", t).strip()


class Sandbox:
    def __init__(self, allow_private=False, allow_search=True):
        self.allow_private = allow_private
        self.allow_search = allow_search
        self.page = None  # {"url", "title", "text", "links", "pos"}
        self.opener = urllib.request.build_opener(_Guard(self), _NoCookies())
        self.opener.addheaders = [("User-Agent", UA), ("Accept", "text/html,text/plain,application/json;q=0.9,*/*;q=0.5"),
                                  ("Accept-Language", "en")]

    # ── Safety ──────────────────────────────────────────────────────
    def check(self, url):
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            raise BlockedURL("Only http and https pages can be opened (got %r)." % (p.scheme or url))
        if p.username or p.password:
            raise BlockedURL("URLs with a username or password are not allowed.")
        if not self.allow_private and _private(p.hostname):
            raise BlockedURL("%s is a local or private-network address; the sandbox refuses those "
                             "(Settings › Web sandbox can allow them)." % p.hostname)

    def _get(self, url, data=None):
        self.check(url)
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        try:
            r = self.opener.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            raise RuntimeError("HTTP %s for %s" % (e.code, url)) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, BlockedURL):
                raise e.reason from None
            raise RuntimeError("Couldn't open %s (%s)" % (url, e.reason)) from None
        with r:
            ctype = (r.headers.get("Content-Type") or "text/html").lower()
            path = urllib.parse.urlsplit(r.geturl()).path.lower()
            if ctype.startswith(("application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml")) \
                    or (ctype.startswith("application/octet-stream") and path.endswith((".pdf", ".doc", ".docx"))):
                raw = r.read(docread.MAX_BYTES + 1)      # a PDF or Word file: read its text
                final = r.geturl()
                try:
                    text, info = docread.extract(path.rsplit("/", 1)[-1] or "file.pdf", raw)
                except docread.DocError as e:
                    raise RuntimeError("%s: %s" % (url, e)) from None
                return final, "text/plain; document=" + info.get("kind", "pdf"), text
            if not ctype.startswith(TEXT_TYPES):
                raise RuntimeError("%s is %s, not a text page; the sandbox only reads text." % (url, ctype.split(";")[0]))
            raw = r.read(MAX_BYTES + 1)
            final = r.geturl()
        m = re.search(r"charset=([\w-]+)", ctype)
        try:
            body = raw[:MAX_BYTES].decode(m.group(1) if m else "utf-8", "replace")
        except LookupError:
            body = raw[:MAX_BYTES].decode("utf-8", "replace")
        return final, ctype, body

    # ── Tools ───────────────────────────────────────────────────────
    def run(self, name, args):
        args = args or {}
        if name == "web_search":
            return self.search(str(args.get("query", "")))
        if name == "open_page":
            return self.open(str(args.get("url", "")).strip())
        if name == "follow_link":
            return self.follow(args.get("number"))
        if name == "read_more":
            return self.more()
        if name == "find_in_page":
            return self.find(str(args.get("text", "")))
        raise RuntimeError("Unknown tool %s" % name)

    def open(self, url):
        if not url:
            raise RuntimeError("No URL given.")
        if "://" not in url:
            url = "https://" + url
        final, ctype, body = self._get(url)
        if "html" in ctype or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            p = _Text(final)
            try:
                p.feed(body)
                p.close()
            except Exception:
                pass
            title, text, links = " ".join(html.unescape(p.title).split()), p.text(), p.links
        elif "json" in ctype:
            try:
                text = json.dumps(json.loads(body), indent=1, ensure_ascii=False)
            except ValueError:
                text = body
            title, links = "", []
        else:
            title, text, links = "", body, []
        self.page = {"url": final, "title": title, "text": text, "links": links, "pos": 0}
        return self._chunk(header=True)

    def _chunk(self, header=False):
        pg = self.page
        start = pg["pos"]
        part = pg["text"][start:start + PAGE_CHARS]
        pg["pos"] = start + len(part)
        left = len(pg["text"]) - pg["pos"]
        out = []
        if header:
            out.append("URL: %s" % pg["url"])
            if pg["title"]:
                out.append("Title: %s" % pg["title"])
            out.append("")
        out.append(part or "(no readable text)")
        if left > 0:
            out.append("\n… %d more characters — call read_more to continue." % left)
        if header and pg["links"]:
            out.append("\nLinks (use follow_link with the number):")
            for i, (u, t) in enumerate(pg["links"][:MAX_LINKS], 1):
                out.append("[%d] %s — %s" % (i, t, u))
            if len(pg["links"]) > MAX_LINKS:
                out.append("… and %d more links" % (len(pg["links"]) - MAX_LINKS))
        return "\n".join(out)

    def follow(self, n):
        if not self.page:
            raise RuntimeError("No page is open yet. Use open_page first.")
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise RuntimeError("follow_link needs a link number.") from None
        if not 1 <= n <= len(self.page["links"]):
            raise RuntimeError("There is no link [%d] on %s." % (n, self.page["url"]))
        return self.open(self.page["links"][n - 1][0])

    def more(self):
        if not self.page:
            raise RuntimeError("No page is open yet. Use open_page first.")
        if self.page["pos"] >= len(self.page["text"]):
            return "(end of page)"
        return self._chunk()

    def find(self, needle):
        if not self.page:
            raise RuntimeError("No page is open yet. Use open_page first.")
        if not needle.strip():
            raise RuntimeError("find_in_page needs some text to look for.")
        text, out = self.page["text"], []
        for m in re.finditer(re.escape(needle), text, re.I):
            a, b = max(0, m.start() - 200), min(len(text), m.end() + 200)
            out.append("…" + " ".join(text[a:b].split()) + "…")
            if len(out) >= 8:
                break
        return ("%d match(es) on %s:\n\n" % (len(out), self.page["url"]) + "\n\n".join(out)) if out else \
            "No matches for %r on %s." % (needle, self.page["url"])

    def search(self, query):
        if not self.allow_search:
            raise RuntimeError("Web search is turned off in Settings › Web sandbox.")
        if not query.strip():
            raise RuntimeError("web_search needs a query.")
        data = urllib.parse.urlencode({"q": query}).encode()
        _final, _ctype, body = self._get("https://html.duckduckgo.com/html/", data=data)
        results = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                             body, re.S):
            href, title, rest = m.groups()
            href = html.unescape(href)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            url = q.get("uddg", [href])[0]
            if url.startswith("//"):
                url = "https:" + url
            if "duckduckgo.com/y.js" in url:  # ads
                continue
            snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
            clean = lambda s: " ".join(html.unescape(re.sub(r"<[^>]+>", "", s or "")).split())
            results.append((clean(title), url, clean(snip.group(1)) if snip else ""))
            if len(results) >= 8:
                break
        if not results:
            return "No results for %r (or the search page changed)." % query
        lines = ["Results for %r:" % query, ""]
        for i, (t, u, s) in enumerate(results, 1):
            lines.append("%d. %s\n   %s\n   %s" % (i, t, u, s))
        return "\n".join(lines)
