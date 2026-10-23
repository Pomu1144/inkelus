"""Fill in a Word (.docx) template, keeping everything about how it looks.

The model never writes Word XML. It gets an outline of the template — its paragraphs (P1, P2…) and tables (T1,
T2…, with their header rows and example rows) — and answers with a short list of changes:

  {"set": "P5", "text": "…"}                        new text for a paragraph (its style and font kept)
  {"after": "P9", "paragraphs": ["…", {"text": "…", "code": true}, {"text": "…", "bullet": true}]}
  {"table": "T1", "rows": [["…", "…", "…"], …]}     the table's rows below its header, in place of the examples
  {"delete": "P7"}                                  remove a paragraph (e.g. an instruction to the writer)

The app applies them to a copy of the .docx: table rows are copies of the template's own example row (borders,
widths, shading, fonts), new paragraphs take the document's body font, and the header, footer, styles, numbering
and everything not mentioned stay exactly as they were. Standard library only.
"""
import copy
import io
import re
import zipfile
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

PROMPT = ("Word templates. These are the person's own Word documents (company templates) that you can fill in. When "
          "they ask for a document one of them covers (design doc, task design, test plan…), call fill_word_template "
          "instead of writing the document in chat: it produces a real Word file in the template's exact formatting. "
          "Each template is listed as an outline: paragraphs P1, P2… and tables T1, T2… Italic paragraphs are usually "
          "instructions or examples for the writer: replace them with the real content (set), or delete the ones that "
          "are only instructions. Table example rows are replaced by the rows you give. Use code paragraphs for code "
          "snippets. Find the facts first (read the project's files and Git changes if you have project tools) and "
          "don't invent file names. After it's made, tell the person in a few lines what you filled in; they download "
          "the file from the chat.")

TOOL = {"type": "function", "function": {
    "name": "fill_word_template",
    "description": "Make a Word document by filling in one of the person's Word templates, keeping its formatting. "
                   "Refer to the template's paragraphs and tables by the ids in its outline (P5, T1…).",
    "parameters": {"type": "object", "required": ["template", "file_name", "changes"], "properties": {
        "template": {"type": "string", "description": "The template's name, exactly as in its outline."},
        "file_name": {"type": "string", "description": "Name for the new document, e.g. \"Task Design - LOAN-482.docx\"."},
        "changes": {"type": "array", "description":
                    "In order. Each is one of: {\"set\": \"P5\", \"text\": \"…\"} (new text, \\n for line breaks); "
                    "{\"after\": \"P6\", \"paragraphs\": [\"text\", {\"text\": \"code\", \"code\": true}, {\"text\": \"…\", \"bullet\": true}]}; "
                    "{\"table\": \"T1\", \"rows\": [[\"cell\", \"cell\", …], …]} (rows below the header, replacing the examples; "
                    "\\n inside a cell for several lines); {\"delete\": \"P8\"}.",
                    "items": {"type": "object"}}}}}}
W = "{%s}" % W_NS
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
MAX_OUTLINE = 14000                   # characters of outline shown to the model


class FillError(ValueError):
    pass


def _load(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise FillError("That isn't a Word .docx file (if it's an old .doc, open it in Word and save it as .docx).") from None
    text = xml.decode("utf-8")
    for prefix, uri in re.findall(r'xmlns:(\w+)="([^"]+)"', text):
        ET.register_namespace(prefix, uri)    # keep Word's own prefixes so the file reads back the same
    return z, text, ET.fromstring(xml)


def _text(p):
    out = []
    for el in p.iter():
        if el.tag == W + "t":
            out.append(el.text or "")
        elif el.tag == W + "tab":
            out.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def _italic(p):
    runs = [r for r in p.iter(W + "r") if "".join(t.text or "" for t in r.iter(W + "t")).strip()]
    return bool(runs) and all(r.find("%srPr/%si" % (W, W)) is not None and
                              r.find("%srPr/%si" % (W, W)).get(W + "val", "true") not in ("0", "false") for r in runs)


def _style(p):
    s = p.find("%spPr/%spStyle" % (W, W))
    return s.get(W + "val", "") if s is not None else ""


def _looks_like_heading(p):
    """A heading by style, or by hand: bold, coloured or larger text on its own (templates often do that)."""
    if re.match(r"(Heading|Title|Subtitle|TOC)", _style(p) or "-"):
        return True
    rpr = _first_rpr(p)
    if rpr is None:
        return False
    sz = rpr.find(W + "sz")
    color = rpr.find(W + "color")
    bold = rpr.find(W + "b") is not None and rpr.find(W + "b").get(W + "val", "true") not in ("0", "false")
    big = sz is not None and (sz.get(W + "val") or "0").isdigit() and int(sz.get(W + "val")) > 24
    coloured = color is not None and (color.get(W + "val") or "auto").lower() not in ("auto", "000000")
    return big or coloured or (bold and len(_text(p).strip()) < 80)


def _blocks(root):
    """[(id, element, parent)] for the body's paragraphs and tables, in order (content controls opened up)."""
    body = root.find(W + "body")
    out, np, nt = [], 0, 0

    def walk(parent):
        nonlocal np, nt
        for el in list(parent):
            if el.tag == W + "p":
                np += 1
                out.append(("P%d" % np, el, parent))
            elif el.tag == W + "tbl":
                nt += 1
                out.append(("T%d" % nt, el, parent))
            elif el.tag == W + "sdt":
                content = el.find(W + "sdtContent")
                if content is not None:
                    walk(content)
    walk(body)
    return out


W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"


def _fresh(el):
    """A copy of el without Word's paragraph ids (Word wants them unique)."""
    el = copy.deepcopy(el)
    for x in el.iter():
        for a in (W14 + "paraId", W14 + "textId"):
            x.attrib.pop(a, None)
    return el


def _rows(tbl):
    return [r for r in tbl if r.tag == W + "tr"]


def _header_rows(tbl):
    """How many rows at the top are headers: rows marked "repeat as header", else the first row."""
    rows = _rows(tbl)
    n = 0
    for r in rows:
        if r.find("%strPr/%stblHeader" % (W, W)) is not None:
            n += 1
        else:
            break
    return n or (1 if len(rows) > 1 else 0)


def _cells(tr):
    return [c for c in tr if c.tag == W + "tc"]


def _cell_text(tc):
    return "\n".join(_text(p) for p in tc.iter(W + "p")).strip()


def outline(data):
    """The template as the model sees it: numbered paragraphs and tables, with what's italic (examples and
    instructions to the writer) marked."""
    _z, _x, root = _load(data)
    lines = []
    for bid, el, _parent in _blocks(root):
        if bid[0] == "P":
            t = _text(el).strip()
            st = _style(el)
            tags = []
            if st:
                tags.append(st)
            if t and _italic(el):
                tags.append("italic")
            lines.append("%s%s: %s" % (bid, " [%s]" % ", ".join(tags) if tags else "", t.replace("\n", " / ") if t else "(empty)"))
        else:
            rows = _rows(el)
            hn = _header_rows(el)
            head = [" | ".join(_cell_text(c).replace("\n", " ") for c in _cells(r)) for r in rows[:hn]]
            ncol = max((len(_cells(r)) for r in rows), default=0)
            lines.append("%s: table, %d columns. Header: %s" % (bid, ncol, " // ".join(head) or "(none)"))
            ex = rows[hn:]
            for r in ex[:6]:
                cells = [_cell_text(c).replace("\n", " / ") for c in _cells(r)]
                lines.append("    example row: " + " | ".join(cells))
            if len(ex) > 6:
                lines.append("    … %d more example rows" % (len(ex) - 6))
    text = "\n".join(lines)
    if len(text) > MAX_OUTLINE:
        text = text[:MAX_OUTLINE] + "\n… (outline cut short)"
    return text


# ── building new content ─────────────────────────────────────────────────
def _plain_rpr(rpr, italic=False, bold=None):
    """A copy of a run's properties for real content: italic (example text) removed unless asked for."""
    rpr = copy.deepcopy(rpr) if rpr is not None else ET.Element(W + "rPr")
    for tag in ("i", "iCs") if not italic else ():
        for x in rpr.findall(W + tag):
            rpr.remove(x)
    if italic and rpr.find(W + "i") is None:
        rpr.append(ET.Element(W + "i"))
    if bold is not None:
        for x in rpr.findall(W + "b"):
            rpr.remove(x)
        if bold:
            rpr.insert(0, ET.Element(W + "b"))
    for x in rpr.findall(W + "vanish"):
        rpr.remove(x)
    return rpr


def _run(text, rpr):
    r = ET.Element(W + "r")
    if rpr is not None and len(rpr):
        r.append(copy.deepcopy(rpr))
    parts = text.split("\t")
    for i, part in enumerate(parts):
        if i:
            r.append(ET.Element(W + "tab"))
        if part:
            t = ET.SubElement(r, W + "t")
            t.text = part
            if part != part.strip():
                t.set(XML_SPACE, "preserve")
    return r


def _first_rpr(p):
    for r in p.iter(W + "r"):
        if "".join(t.text or "" for t in r.iter(W + "t")).strip():
            return r.find(W + "rPr")
    r = p.find(W + "r")
    return r.find(W + "rPr") if r is not None else (p.find("%spPr/%srPr" % (W, W)))


def _set_text(p, text, rpr):
    """Replace a paragraph's content with text (line breaks as breaks), keeping its paragraph properties and
    any picture in it."""
    keep = (W + "drawing", W + "pict", W + "object")
    for el in list(p):
        if el.tag == W + "pPr":
            continue
        if el.tag == W + "r" and any(x.tag in keep for x in el.iter()):
            continue
        p.remove(el)
    for i, line in enumerate(text.split("\n")):
        r = _run(line, rpr)
        if i:
            r.insert(1 if len(r) and r[0].tag == W + "rPr" else 0, ET.Element(W + "br"))
        p.append(r)


def _code_rpr(base):
    rpr = _plain_rpr(base)
    for tag in ("rFonts", "sz", "szCs", "b", "color"):
        for x in rpr.findall(W + tag):
            rpr.remove(x)
    f = ET.Element(W + "rFonts")
    for k in ("ascii", "hAnsi", "cs", "eastAsia"):
        f.set(W + k, "Consolas")
    rpr.insert(0, f)
    sz = ET.SubElement(rpr, W + "sz")
    sz.set(W + "val", "18")
    return rpr


def _new_para(spec, body_ppr, body_rpr, styles):
    if isinstance(spec, str):
        spec = {"text": spec}
    p = ET.Element(W + "p")
    text = str(spec.get("text") or "")
    if spec.get("code"):
        ppr = ET.SubElement(p, W + "pPr")
        sp = ET.SubElement(ppr, W + "spacing")
        sp.set(W + "before", "60")
        sp.set(W + "after", "120")
        shd = ET.SubElement(ppr, W + "shd")
        shd.set(W + "val", "clear")
        shd.set(W + "color", "auto")
        shd.set(W + "fill", "F2F2F2")
        lines = text.split("\n")
        _set_text(p, "\n".join(lines), _code_rpr(body_rpr))
        return p
    if body_ppr is not None:
        p.append(copy.deepcopy(body_ppr))
    if spec.get("bullet"):
        if "ListBullet" in styles:
            ppr = p.find(W + "pPr")
            if ppr is None:
                ppr = ET.Element(W + "pPr")
                p.insert(0, ppr)
            for x in ppr.findall(W + "pStyle"):
                ppr.remove(x)
            st = ET.Element(W + "pStyle")
            st.set(W + "val", "ListBullet")
            ppr.insert(0, st)
        else:
            text = "• " + text
    _set_text(p, text, _plain_rpr(body_rpr, italic=bool(spec.get("italic")), bold=spec.get("bold")))
    return p


def _fill_table(tbl, rows, keep_examples=False):
    trs = _rows(tbl)
    hn = _header_rows(tbl)
    examples = trs[hn:]
    if not examples:                                       # only a header: copy it as the pattern, without its bold
        examples = [copy.deepcopy(trs[-1])] if trs else []
        for b in (x for r in examples for x in r.iter(W + "b")):
            b.set(W + "val", "0")
    if not examples:
        raise FillError("That table has no rows to copy.")
    pattern = examples[0]
    # the last example row is usually the "……" filler row: use the first real-looking one as the pattern
    for r in examples:
        if any(_cell_text(c).strip(". …") for c in _cells(r)):
            pattern = r
            break
    anchor_index = list(tbl).index(examples[0]) if examples[0] in list(tbl) else len(list(tbl))
    if not keep_examples:
        for r in examples:
            if r in list(tbl):
                tbl.remove(r)
    ncols = len(_cells(pattern))
    for k, values in enumerate(rows or [[""] * ncols]):
        values = [str(v if v is not None else "") for v in (values if isinstance(values, list) else [values])]
        values = (values + [""] * ncols)[:ncols]
        tr = _fresh(pattern)
        for tr_el in list(tr):
            if tr_el.tag == W + "trPr":
                for x in tr_el.findall(W + "tblHeader"):
                    tr_el.remove(x)
        for tc, val in zip(_cells(tr), values):
            paras = [p for p in tc if p.tag == W + "p"]
            first = paras[0] if paras else ET.SubElement(tc, W + "p")
            rpr = _plain_rpr(_first_rpr(first))
            for extra in paras[1:]:
                tc.remove(extra)
            for t in [x for x in tc if x.tag == W + "tbl"]:
                tc.remove(t)
            lines = val.split("\n")
            _set_text(first, lines[0], rpr)
            at = list(tc).index(first)
            for j, line in enumerate(lines[1:], 1):
                np_ = ET.Element(W + "p")
                if first.find(W + "pPr") is not None:
                    np_.append(copy.deepcopy(first.find(W + "pPr")))
                _set_text(np_, line, rpr)
                tc.insert(at + j, np_)
        tbl.insert(anchor_index + k, tr)


def fill(data, changes):
    """The template (bytes) with the changes applied, as a new .docx (bytes)."""
    z, xml_text, root = _load(data)
    blocks = _blocks(root)
    by_id = {bid: (el, parent) for bid, el, parent in blocks}
    styles = ""
    try:
        styles = z.read("word/styles.xml").decode("utf-8", "replace")
    except KeyError:
        pass
    styles = set(re.findall(r'w:styleId="([^"]+)"', styles))
    def body_look(p):
        ppr = p.find(W + "pPr")
        ppr = copy.deepcopy(ppr) if ppr is not None else None
        if ppr is not None:
            for x in list(ppr):
                if x.tag in (W + "numPr", W + "rPr", W + "sectPr", W + "pStyle"):
                    ppr.remove(x)
        return ppr, _plain_rpr(_first_rpr(p), bold=False)
    # the document's ordinary body text: the first paragraph with text that isn't a heading (instructions count)
    body_ppr = body_rpr = None
    for bid, el, _ in blocks:
        if bid[0] == "P" and _text(el).strip() and not _looks_like_heading(el):
            body_ppr, body_rpr = body_look(el)
            break
    if not isinstance(changes, list):
        raise FillError("changes must be a list.")
    done, deletes = [], []
    for ch in changes:
        if not isinstance(ch, dict):
            raise FillError("Each change must be an object like {\"set\": \"P5\", \"text\": \"…\"}.")
        if "set" in ch:
            el, _ = by_id.get(str(ch["set"]), (None, None))
            if el is None or el.tag != W + "p":
                raise FillError("There's no paragraph %s in the template." % ch["set"])
            keep_italic = bool(ch.get("italic"))
            _set_text(el, str(ch.get("text") or ""), _plain_rpr(_first_rpr(el), italic=keep_italic, bold=ch.get("bold")))
            done.append("set %s" % ch["set"])
        elif "after" in ch:
            el, parent = by_id.get(str(ch["after"]), (None, None))
            if el is None:
                raise FillError("There's no paragraph or table %s in the template." % ch["after"])
            paras = ch.get("paragraphs")
            paras = paras if isinstance(paras, list) else [paras] if paras else []
            at = list(parent).index(el) + 1
            # new text looks like the paragraph it follows when that's body text, else like the document's body
            ppr, rpr = body_look(el) if el.tag == W + "p" and _text(el).strip() and not _looks_like_heading(el) else (body_ppr, body_rpr)
            for k, spec in enumerate(paras):
                parent.insert(at + k, _new_para(spec, ppr, rpr, styles))
            done.append("%d paragraph(s) after %s" % (len(paras), ch["after"]))
        elif "table" in ch:
            el, _ = by_id.get(str(ch["table"]), (None, None))
            if el is None or el.tag != W + "tbl":
                raise FillError("There's no table %s in the template." % ch["table"])
            rows = ch.get("rows") or []
            if not isinstance(rows, list):
                raise FillError("rows must be a list of rows, each a list of cell texts.")
            _fill_table(el, rows, bool(ch.get("keep_examples")))
            done.append("%d row(s) in %s" % (len(rows), ch["table"]))
        elif "delete" in ch:
            deletes.append(str(ch["delete"]))
        else:
            raise FillError("Unknown change %s: use set, after, table or delete." % list(ch)[:1])
    for bid in deletes:                                     # last, so ids used by other changes still point right
        el, parent = by_id.get(bid, (None, None))
        if el is not None and el in list(parent):
            parent.remove(el)
            done.append("deleted %s" % bid)
    xml = ET.tostring(root, encoding="unicode")
    # put Word's original root tag back: it declares every namespace (mc:Ignorable needs them all)
    orig_root = re.search(r"<w:document\b[^>]*>", xml_text)
    new_root = re.match(r"<w:document\b[^>]*>", xml)
    if orig_root and new_root:
        tag = orig_root.group(0)
        have = set(re.findall(r'xmlns:(\w+)=', tag))
        extra = "".join(' xmlns:%s="%s"' % (pfx, uri) for pfx, uri in re.findall(r'xmlns:(\w+)="([^"]+)"', new_root.group(0)) if pfx not in have)
        if extra:                                      # namespaces only declared deeper in the original
            tag = tag[:-1].rstrip("/") + extra + ">"
        xml = tag + xml[new_root.end():]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in z.infolist():
            if item.filename == "word/document.xml":
                zo.writestr(item, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + xml)
            else:
                zo.writestr(item, z.read(item.filename))
    return out.getvalue(), done
