"""The Library: documents and templates kept as plain files in the data folder.

documents/   reference material a chat can hand to the model (notes, specs, code…)
templates/   examples of how an answer should be laid out; the model follows them

Everything is stored as UTF-8 text so it can be opened and edited outside the
app too. Word files (.docx) are converted to text when imported.
"""
import os
import re
import zipfile
from xml.etree import ElementTree

KINDS = ("documents", "templates")
MAX_IMPORT = 2 * 1024 * 1024        # bytes read from an imported file
DOC_LIMIT = 24000                   # chars of one document sent to the model
DOCS_TOTAL = 60000                  # chars of all documents together
TEMPLATES_TOTAL = 16000             # chars of templates in "auto" mode

TEXT_TYPES = [("Text and documents", "*.txt *.md *.markdown *.csv *.tsv *.json *.xml *.html *.htm *.yaml *.yml "
                                     "*.ini *.cfg *.toml *.log *.py *.js *.ts *.java *.c *.cpp *.h *.cs *.go "
                                     "*.rs *.rb *.php *.sql *.sh *.ps1 *.css *.docx"),
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


class Library:
    def __init__(self, root):
        self.root = root
        fresh = not os.path.isdir(self.dir("templates"))
        for k in KINDS:
            os.makedirs(self.dir(k), exist_ok=True)
        if fresh:  # first run: a few examples so it's clear what a template is
            for name, text in SAMPLE_TEMPLATES.items():
                self.save("templates", name, text)

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
        try:
            os.remove(self.path(kind, name))
        except OSError:
            pass

    def rename(self, kind, old, new):
        src, dst = self.path(kind, old), self.path(kind, new)
        if src == dst:
            return os.path.basename(dst)
        if os.path.exists(dst):
            raise ValueError("%s already exists." % os.path.basename(dst))
        os.replace(src, dst)
        return os.path.basename(dst)

    def import_file(self, kind, src):
        """Copy a file into the library as text. Returns the stored name."""
        base = os.path.basename(src)
        if base.lower().endswith(".docx"):
            text = docx_text(src)
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
        return self.save(kind, name, text)

    # ── What the model sees ─────────────────────────────────────────────
    def context_prompt(self, docs, template):
        """System-prompt text for the chosen documents and template.

        template: "" or None = no template, "auto" = every template (the model
        picks the one that fits), otherwise one template's name."""
        parts = []
        have = set(self.names("documents"))
        docs = [d for d in docs or [] if d in have]
        if docs:
            budget = DOCS_TOTAL
            blocks = []
            for d in docs:
                text = self.read("documents", d).strip()
                cut = min(DOC_LIMIT, max(budget, 0))
                if len(text) > cut:
                    text = text[:cut] + "\n…[document truncated]"
                budget -= len(text)
                blocks.append('<document name="%s">\n%s\n</document>' % (d, text))
            parts.append("The user has shared these reference documents. Use them when they're relevant, "
                         "quote them accurately, and say which document you used.\n\n" + "\n\n".join(blocks))
        tnames = self.names("templates")
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
        elif template and template in tnames:
            parts.append('Format your answer using this template: keep its sections, headings, order and '
                         'style, and fill in the {{placeholders}}. Don\'t add sections it doesn\'t have.\n\n'
                         '<template name="%s">\n%s\n</template>' % (template, self.read("templates", template).strip()))
        return "\n\n".join(parts)


def clean_name(name):
    """A safe file name: no folders, no characters Windows refuses."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip()).strip(". ")
    if name and "." not in name:
        name += ".md"
    return name[:120]


def docx_text(path):
    """Plain text from a Word document, with headings and list items marked up lightly."""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(ns + "p"):
        text = "".join(t.text or "" for t in p.iter(ns + "t"))
        style = p.find("%spPr/%spStyle" % (ns, ns))
        style = style.get(ns + "val", "") if style is not None else ""
        if p.find("%spPr/%snumPr" % (ns, ns)) is not None and text:
            text = "- " + text
        m = re.match(r"Heading(\d)", style)
        if m and text:
            text = "#" * int(m.group(1)) + " " + text
        elif style == "Title" and text:
            text = "# " + text
        out.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"
