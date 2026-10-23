"""The Library: documents and templates kept as plain files in the data folder.

documents/   reference material a chat can hand to the model (notes, specs, code…)
templates/   examples of how an answer should be laid out; the model follows them
standards/   documentation standards: layouts the model MUST follow whenever it writes documentation
             (READMEs, guides, API docs…), in every chat. In a project, a doc file that matches a
             standard's file patterns is checked: a missing required section stops the write.

Everything is stored as UTF-8 text so it can be opened and edited outside the
app too. PDF and Word files (.pdf, .docx, .doc) are converted to text when imported (docread.py);
an imported PDF's original is kept too (documents/.originals/), so a model that reads PDF files
itself (Bedrock) can be given the file as well as its text.
"""
import fnmatch
import json
import os
import re

from . import docread

KINDS = ("documents", "templates", "standards")
STANDARDS_TOTAL = 16000             # chars of documentation standards in every chat
RULES = ".rules.json"               # per-standard settings, next to the standard files
MAX_IMPORT = 2 * 1024 * 1024        # bytes read from an imported file
DOC_LIMIT = 24000                   # chars of one document sent to the model
DOCS_TOTAL = 60000                  # chars of all documents together
ORIGINALS = ".originals"            # imported PDFs, next to their text: documents/.originals/<name>.pdf
NATIVE_MAX = 4_500_000              # largest PDF handed to a model as a file (Bedrock's limit per document)
TEMPLATES_TOTAL = 16000             # chars of templates in "auto" mode

TEXT_TYPES = [("Text and documents", "*.txt *.md *.markdown *.csv *.tsv *.json *.xml *.html *.htm *.yaml *.yml "
                                     "*.ini *.cfg *.toml *.log *.py *.js *.ts *.java *.c *.cpp *.h *.cs *.go "
                                     "*.rs *.rb *.php *.sql *.sh *.ps1 *.css *.docx *.doc *.pdf"),
              ("All files", "*.*")]

SAMPLE_TEMPLATES = {
    "Meeting notes.md": """# Meeting notes: {{topic}}

**Date:** {{date}}
**Attendees:** {{names}}

## Summary
One or two sentences on what the meeting was about and what was decided.

## Decisions
- {{decision}}

## Action items
| Owner | Task | Due |
|---|---|---|
| {{name}} | {{task}} | {{date}} |

## Open questions
- {{question}}
""",
    "Bug report.md": """## Bug: {{short title}}

**Severity:** Low / Medium / High / Critical
**Environment:** {{OS, app version, browser}}

### Steps to reproduce
1. {{step}}
2. {{step}}

### Expected result
{{what should happen}}

### Actual result
{{what happens instead, with error messages in a code block}}

### Notes
{{logs, screenshots, suspected cause}}
""",
    "Email.md": """Subject: {{clear, specific subject}}

Hi {{name}},

{{One-sentence purpose of the email.}}

{{Short body: two or three paragraphs at most. Use a bulleted list for several items.}}

{{Clear next step or request, with a date if there is one.}}

Best regards,
{{sender}}
""",
}


SAMPLE_STANDARD = ("README standard.md", """# {{Project name}}

{{One or two sentences: what it is and who it's for.}}

## Getting started
{{Requirements, install and the first command to run, as numbered steps with code blocks.}}

## Usage
{{The most common tasks, each with a short example.}}

## Configuration
{{Settings and environment variables, as a table: name, default, what it does.}}

## Troubleshooting
{{Common problems and their fixes.}}

## License
{{License name.}}
""", {"use_for": "README files", "files": "README.md, readme.md", "active": False})


class Library:
    def __init__(self, root):
        self.root = root
        fresh = not os.path.isdir(self.dir("templates"))
        fresh_std = not os.path.isdir(self.dir("standards"))
        for k in KINDS:
            os.makedirs(self.dir(k), exist_ok=True)
        if fresh:  # first run: a few examples so it's clear what a template is
            for name, text in SAMPLE_TEMPLATES.items():
                self.save("templates", name, text)
        if fresh_std:  # an example standard, switched off until the user turns it on
            name, text, meta = SAMPLE_STANDARD
            self.save("standards", name, text)
            self.set_meta(name, meta)

    # per-standard settings: what it's for, which files it applies to, whether the AI must follow it
    def metas(self):
        try:
            with open(os.path.join(self.dir("standards"), RULES), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def meta(self, name):
        m = self.metas().get(name) or {}
        return {"use_for": str(m.get("use_for") or ""), "files": str(m.get("files") or ""),
                "active": m.get("active", True) is not False}

    def set_meta(self, name, m, old=None):
        d = self.metas()
        if old and old in d:
            d[name] = d.pop(old)
        if m is not None:
            d[name] = {"use_for": str(m.get("use_for") or "")[:300], "files": str(m.get("files") or "")[:500],
                       "active": bool(m.get("active", True))}
        d = {k: v for k, v in d.items() if os.path.isfile(os.path.join(self.dir("standards"), k))}
        with open(os.path.join(self.dir("standards"), RULES), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

    def active_standards(self):
        return [n for n in self.names("standards") if self.meta(n)["active"]]

    def standards_for_path(self, rel):
        """Active standards whose file patterns match a project file path (e.g. README.md, docs/**/*.md)."""
        rel = rel.replace("\\", "/")
        base = rel.rsplit("/", 1)[-1]
        out = []
        for n in self.active_standards():
            pats = [p.strip() for p in re.split(r"[,;\n]", self.meta(n)["files"]) if p.strip()]
            for p in pats:
                p = p.replace("\\", "/")
                if fnmatch.fnmatch(rel, p) or ("/" not in p and fnmatch.fnmatch(base, p)) or \
                        (p.startswith("**/") and fnmatch.fnmatch(rel, p[3:])):
                    out.append(n)
                    break
        return out

    def dir(self, kind):
        assert kind in KINDS
        return os.path.join(self.root, kind)

    def names(self, kind):
        try:
            files = [n for n in os.listdir(self.dir(kind))
                     if not n.startswith(".") and os.path.isfile(os.path.join(self.dir(kind), n))]
        except OSError:
            return []
        return sorted(files, key=str.lower)

    def path(self, kind, name):
        name = clean_name(name)
        if not name:
            raise ValueError("Give it a name.")
        return os.path.join(self.dir(kind), name)

    def read(self, kind, name):
        try:
            with open(self.path(kind, name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def save(self, kind, name, text):
        p = self.path(kind, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return os.path.basename(p)

    def delete(self, kind, name):
        for p in (self.path(kind, name), self.original(kind, name), self.original(kind, name, "docx")):
            try:
                os.remove(p)
            except OSError:
                pass

    def original(self, kind, name, ext="pdf"):
        """Where the original file of an imported document or template is kept (it may not exist): a PDF for
        documents, the Word file for Word templates."""
        return os.path.join(self.dir(kind), ORIGINALS, clean_name(name) + "." + ext)

    def word_templates(self, template="auto"):
        """{template name: path of its .docx} for the Word templates a chat can fill in."""
        names = self.names("templates") if template == "auto" else [template] if template else []
        return {n: self.original("templates", n, "docx") for n in names if os.path.isfile(self.original("templates", n, "docx"))}

    def pdf_originals(self, docs):
        """{document name: path} for the chosen documents that came from a PDF small enough to send as a file."""
        out = {}
        for d in docs or []:
            p = self.original("documents", d)
            try:
                if 0 < os.path.getsize(p) <= NATIVE_MAX:
                    out[d] = p
            except OSError:
                pass
        return out

    def rename(self, kind, old, new):
        src, dst = self.path(kind, old), self.path(kind, new)
        if src == dst:
            return os.path.basename(dst)
        if os.path.exists(dst):
            raise ValueError("%s already exists." % os.path.basename(dst))
        os.replace(src, dst)
        for ext in ("pdf", "docx"):
            if os.path.exists(self.original(kind, old, ext)):
                os.replace(self.original(kind, old, ext), self.original(kind, os.path.basename(dst), ext))
        if kind == "standards":
            self.set_meta(os.path.basename(dst), None, old=os.path.basename(src))
        return os.path.basename(dst)

    def import_file(self, kind, src):
        """Copy a file into the library as text. Returns the stored name."""
        base = os.path.basename(src)
        with open(src, "rb") as f:
            head = f.read(8)
        doc = docread.kind(base, head)
        if doc:
            try:
                text, info = docread.extract_file(src)
            except docread.DocError as e:
                raise ValueError("%s: %s" % (base, e)) from None
            if doc == "pdf":
                text = "Text of the PDF %s (%d page%s).\n\n%s" % (base, info["pages"], "" if info["pages"] == 1 else "s", text)
            base = os.path.splitext(base)[0] + ".md"
        else:
            with open(src, "rb") as f:
                raw = f.read(MAX_IMPORT + 1)
            if b"\0" in raw[:4096]:
                raise ValueError("%s isn't a text file." % base)
            text = raw[:MAX_IMPORT].decode("utf-8", errors="replace")
        name, n = base, 2
        while os.path.exists(self.path(kind, name)):
            stem, ext = os.path.splitext(base)
            name, n = "%s (%d)%s" % (stem, n, ext), n + 1
        stored = self.save(kind, name, text)
        keep = "pdf" if doc == "pdf" else "docx" if doc == "docx" and kind == "templates" else None
        if keep:                               # the PDF for models that read PDFs; the Word file to fill in
            import shutil
            os.makedirs(os.path.dirname(self.original(kind, stored, keep)), exist_ok=True)
            shutil.copyfile(src, self.original(kind, stored, keep))
        return stored

    # ── What the model sees ─────────────────────────────────────────────
    def context_prompt(self, docs, template, as_files=()):
        """System-prompt text for the chosen documents and template.

        template: "" or None = no template, "auto" = every template (the model
        picks the one that fits), otherwise one template's name.
        as_files: documents the model gets as the original PDF file instead of their text."""
        parts = []
        have = set(self.names("documents"))
        docs = [d for d in docs or [] if d in have]
        if docs:
            budget = DOCS_TOTAL
            blocks = []
            for d in docs:
                if d in as_files:
                    blocks.append('<document name="%s">(attached as the original PDF file: read it there)</document>' % d)
                    continue
                text = self.read("documents", d).strip()
                cut = min(DOC_LIMIT, max(budget, 0))
                if len(text) > cut:
                    text = text[:cut] + "\n…[document truncated]"
                budget -= len(text)
                blocks.append('<document name="%s">\n%s\n</document>' % (d, text))
            parts.append("The user has shared these reference documents. Use them when they're relevant, "
                         "quote them accurately, and say which document you used.\n\n" + "\n\n".join(blocks))
        word = self.word_templates(template)
        tnames = [t for t in self.names("templates") if t not in word]
        if word:
            from . import docxfill
            blocks = []
            for t, path in word.items():
                try:
                    with open(path, "rb") as f:
                        blocks.append('<word_template name="%s">\n%s\n</word_template>' % (t, docxfill.outline(f.read())))
                except (OSError, ValueError) as e:
                    blocks.append('<word_template name="%s">(can\'t be read: %s)</word_template>' % (t, e))
            parts.append(docxfill.PROMPT + "\n\n" + "\n\n".join(blocks))
        if template == "auto" and tnames:
            blocks, budget = [], TEMPLATES_TOTAL
            for t in tnames:
                text = self.read("templates", t).strip()
                if len(text) > budget:
                    blocks.append('<template name="%s">(too long to include)</template>' % t)
                    continue
                budget -= len(text)
                blocks.append('<template name="%s">\n%s\n</template>' % (t, text))
            parts.append("Formatting templates. When the user asks for something one of these templates "
                         "covers (judge by its name and content), lay your answer out exactly like that "
                         "template: same sections, headings, order and style, with the {{placeholders}} "
                         "filled in. If none fits, answer normally.\n\n" + "\n\n".join(blocks))
        elif template and template in tnames and template not in word:
            parts.append('Format your answer using this template: keep its sections, headings, order and '
                         'style, and fill in the {{placeholders}}. Don\'t add sections it doesn\'t have.\n\n'
                         '<template name="%s">\n%s\n</template>' % (template, self.read("templates", template).strip()))
        std = self.standards_prompt()
        if std:
            parts.append(std)
        return "\n\n".join(parts)

    def standards_prompt(self):
        """The documentation standards, for every chat."""
        names = self.active_standards()
        if not names:
            return ""
        blocks, budget = [], STANDARDS_TOTAL
        for n in names:
            m = self.meta(n)
            text = self.read("standards", n).strip()
            if len(text) > budget:
                text = text[:max(budget, 0)] + "\n…[standard truncated]"
            budget -= len(text)
            attrs = ' name="%s"' % n + (' use_for="%s"' % m["use_for"] if m["use_for"] else "") + \
                (' files="%s"' % m["files"] if m["files"] else "")
            blocks.append("<standard%s>\n%s\n</standard>" % (attrs, text))
        return ("Documentation standards. These are rules, not suggestions: whenever you write or rewrite "
                "documentation (README files, guides, API references, runbooks, design docs, release notes, "
                "changelogs and similar), use the standard that matches it (by its use_for and files) and follow it "
                "exactly: the same sections and headings, in the same order, with the same style, and the "
                "{{placeholders}} filled in. Don't drop or rename sections; if a section doesn't apply, keep it and "
                "say so briefly. If no standard matches, follow the closest one's style.\n\n" + "\n\n".join(blocks))


def clean_name(name):
    """A safe file name: no folders, no characters Windows refuses."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip()).strip(". ")
    if name and "." not in name:
        name += ".md"
    return name[:120]


def docx_text(path):
    """Plain text from a Word document (kept for callers of the old name)."""
    with open(path, "rb") as f:
        return docread.docx_text(f.read())


# ── checking a document against a standard ──────────────────────────────
def _norm(t):
    t = re.sub(r"[*_`]+", "", t).strip().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w{}\s:/&+-]", "", t)).strip()


def required_headings(standard_text):
    """The standard's Markdown headings, as (display text, pattern). {{placeholders}} match anything."""
    out = []
    in_code = False
    for line in standard_text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not m:
            continue
        text = m.group(2).strip()
        norm = _norm(text)
        parts = re.split(r"\{\{[^}]*\}\}", norm)
        if not "".join(parts).strip(" :-"):
            continue            # a heading that is only a placeholder (e.g. "# {{Project name}}") is any title
        pattern = ".*".join(re.escape(p) for p in parts)
        out.append((text, re.compile("^" + pattern + "$")))
    return out


def check_document(text, standard_text):
    """Which of the standard's sections a document is missing, and whether they're in order."""
    req = required_headings(standard_text)
    have = []
    in_code = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            have.append(_norm(m.group(2)))
    missing, positions = [], []
    for display, rx in req:
        idx = next((i for i, h in enumerate(have) if rx.match(h)), None)
        if idx is None:
            missing.append(display)
        else:
            positions.append(idx)
    in_order = positions == sorted(positions)
    return {"missing": missing, "in_order": in_order, "required": [d for d, _ in req]}
