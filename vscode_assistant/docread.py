"""Text from PDF, Word (.docx) and old Word (.doc) files, with the standard library only.

PDF: reads the page tree in order, decodes the content streams (Flate, LZW, ASCII85, ASCIIHex, RunLength),
including compressed object streams (PDF 1.5+) and form XObjects, and maps text through each font's
ToUnicode map or encoding. Files that are encrypted only to restrict printing or copying (an empty open
password) are decrypted (RC4 and AES). Scanned pages are pictures with no text in them: they're reported
as such rather than guessed at (that needs OCR).

.doc: Word 97-2003 binary files, read from their compound-file container through the piece table.
"""
import base64
import hashlib
import re
import struct
import zipfile
import zlib
from xml.etree import ElementTree

MAX_BYTES = 60 * 1024 * 1024       # largest file read
MAX_PAGES = 2000


class DocError(Exception):
    """The file can't be read as a document; the message says why, in plain words."""


def kind(name, head=b""):
    n = (name or "").lower()
    if n.endswith(".pdf") or head.startswith(b"%PDF"):
        return "pdf"
    if n.endswith(".docx") or (head.startswith(b"PK") and n.endswith((".docm", ".dotx"))):
        return "docx"
    if n.endswith((".doc", ".dot")) or head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    return None


def extract(name, data):
    """(text, info) from a document's bytes. info: {"kind", "pages", "scanned_pages"}."""
    k = kind(name, data[:8])
    if k == "pdf":
        return pdf_text(data)
    if k == "docx":
        return docx_text(data), {"kind": "docx"}
    if k == "doc":
        return doc_text(data), {"kind": "doc"}
    raise DocError("%s isn't a PDF or Word document." % name)


def extract_file(path):
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise DocError("That file is too big to read (over %d MB)." % (MAX_BYTES // (1024 * 1024)))
    return extract(path, data)


# ── .docx ────────────────────────────────────────────────────────────────
def docx_text(data):
    """Plain text from a Word document, with headings and list items marked up lightly."""
    import io
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise DocError("That Word file is damaged or isn't really a .docx.") from None
    out = []
    body = root.find(ns + "body")
    for el in (body if body is not None else root):
        if el.tag == ns + "tbl":                       # tables: one row per line, cells split by " | "
            for tr in el.iter(ns + "tr"):
                cells = ["".join(t.text or "" for t in tc.iter(ns + "t")).strip() for tc in tr.iter(ns + "tc")]
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
            continue
        for p in ([el] if el.tag == ns + "p" else el.iter(ns + "p")):
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


# ── .doc (Word 97-2003) ──────────────────────────────────────────────────
class Cfb:
    """A compound file (OLE2) container: the streams by name."""
    FREE, END = 0xFFFFFFFF, 0xFFFFFFFE

    def __init__(self, data):
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise DocError("That isn't an old-style Word file.")
        self.d = data
        self.ssz = 1 << struct.unpack_from("<H", data, 30)[0]
        self.mssz = 1 << struct.unpack_from("<H", data, 32)[0]
        n_fat, dir_start = struct.unpack_from("<II", data, 44)
        self.mini_cut, mini_fat_start, _n_mini, difat_start, n_difat = struct.unpack_from("<IIIII", data, 56)
        difat = list(struct.unpack_from("<109I", data, 76))
        s = difat_start
        for _ in range(n_difat):
            if s >= self.END:
                break
            vals = struct.unpack_from("<%dI" % (self.ssz // 4), self._sector(s))
            difat += vals[:-1]
            s = vals[-1]
        self.fat = []
        for s in difat[:n_fat]:
            if s < self.END:
                self.fat += struct.unpack_from("<%dI" % (self.ssz // 4), self._sector(s))
        dirdata = self._chain(dir_start)
        self.entries = []
        for i in range(0, len(dirdata), 128):
            e = dirdata[i:i + 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[:max(nlen - 2, 0)].decode("utf-16-le", "replace")
            etype = e[66]
            start, size = struct.unpack_from("<IQ", e, 116)
            if self.ssz == 512:
                size &= 0xFFFFFFFF
            self.entries.append((name, etype, start, size))
        root = self.entries[0]
        self.ministream = self._chain(root[2])[:root[3]] if root[1] == 5 else b""
        self.minifat = list(struct.unpack_from("<%dI" % (len(self._chain(mini_fat_start)) // 4), self._chain(mini_fat_start))) \
            if mini_fat_start < self.END else []

    def _sector(self, s):
        off = (s + 1) * self.ssz
        return self.d[off:off + self.ssz]

    def _chain(self, s, limit=1 << 20):
        out, seen = [], 0
        while s < self.END and seen < limit:
            out.append(self._sector(s))
            s = self.fat[s] if s < len(self.fat) else self.END
            seen += 1
        return b"".join(out)

    def stream(self, name):
        for n, etype, start, size in self.entries:
            if n == name and etype == 2:
                if size < self.mini_cut:
                    out, s = [], start
                    while s < self.END and len(out) * self.mssz < size + self.mssz:
                        out.append(self.ministream[s * self.mssz:(s + 1) * self.mssz])
                        s = self.minifat[s] if s < len(self.minifat) else self.END
                    return b"".join(out)[:size]
                return self._chain(start)[:size]
        return None


def doc_text(data):
    """Plain text from a Word 97-2003 .doc file."""
    try:
        cfb = Cfb(data)
    except struct.error:
        raise DocError("That Word file is damaged.") from None
    wd = cfb.stream("WordDocument")
    if not wd or len(wd) < 0x200:
        raise DocError("That file has no Word document in it (it may be another Office file).")
    ident, nfib = struct.unpack_from("<HH", wd, 0)
    if ident != 0xA5EC or nfib < 0xC1:
        raise DocError("That's a Word 6/95 or older file; open it in Word and save it as .docx.")
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    if flags & 0x0100:
        raise DocError("That Word file is password-protected.")
    table = cfb.stream("1Table" if flags & 0x0200 else "0Table")
    if table is None:
        raise DocError("That Word file is damaged (no table stream).")
    pos = 32
    csw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + csw * 2
    cslw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + cslw * 4
    pos += 2                                                  # cbRgFcLcb
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, pos + 33 * 8)
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 1:                       # Prc: formatting we don't need
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 2:
        raise DocError("That Word file is damaged (no piece table).")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from("<%dI" % (n + 1), plc, 0)
    parts = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, (n + 1) * 4 + k * 8 + 2)[0]
        count = cps[k + 1] - cps[k]
        if fc & 0x40000000:
            off = (fc & 0x3FFFFFFF) // 2
            parts.append(wd[off:off + count].decode("cp1252", "replace"))
        else:
            parts.append(wd[fc:fc + 2 * count].decode("utf-16-le", "replace"))
    text = "".join(parts)
    ccp_text = struct.unpack_from("<I", wd, 32 + 2 + csw * 2 + 2 + 3 * 4)[0]   # the main text, before headers/notes
    if 0 < ccp_text <= len(text):
        text = text[:ccp_text]
    return _clean_word(text)


def _clean_word(text):
    # fields: \x13 instructions \x14 shown result \x15 — keep what's shown
    for _ in range(4):                                        # fields can nest
        text = re.sub(r"\x13[^\x13\x14\x15]*\x14([^\x13\x15]*)\x15", r"\1", text)
        text = re.sub(r"\x13[^\x13\x14\x15]*\x15", "", text)
    # tables: each cell ends with \x07, and a row with one more; written as "| a | b |" rows
    text = text.replace("\x07\x07", "\x07\x1e")
    out = []
    for para in text.replace("\r", "\n").split("\n"):
        if "\x07" not in para:
            out.append(para)
            continue
        for row in para.split("\x1e"):
            if "\x07" not in row:                     # text right after a table, in the same paragraph run
                out.append(row)
                continue
            cells = row.split("\x07")
            if cells and cells[-1] == "":
                cells.pop()
            if cells:
                out.append("| " + " | ".join(c.strip() for c in cells) + " |")
    text = "\n".join(out).replace("\x0b", "\n").replace("\x0c", "\n\n")
    text = re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ── PDF: the file ────────────────────────────────────────────────────────
class Ref:
    __slots__ = ("num", "gen")

    def __init__(self, num, gen):
        self.num, self.gen = num, gen

    def __repr__(self):
        return "Ref(%d)" % self.num


class Name(str):
    pass


class Stream:
    def __init__(self, d, raw, num=None):
        self.d, self.raw, self.num = d, raw, num        # num: decrypt with this object's key when decoded


WS = b" \t\r\n\f\x00"
DELIM = b"()<>[]{}/%"


class Lexer:
    def __init__(self, data, pos=0):
        self.d, self.p = data, pos

    def skip(self):
        d, n = self.d, len(self.d)
        while self.p < n:
            c = d[self.p]
            if c in WS:
                self.p += 1
            elif c == 0x25:                                   # % comment
                e = d.find(b"\n", self.p)
                self.p = n if e < 0 else e + 1
            else:
                return

    def token(self):
        """The next token: (kind, value). kinds: num, name, str, hex, kw, '[', ']', '<<', '>>', eof."""
        self.skip()
        d, p = self.d, self.p
        if p >= len(d):
            return ("eof", None)
        c = d[p:p + 1]
        if c == b"/":
            m = re.compile(rb"/([^\s()<>\[\]{}/%]*)").match(d, p)
            self.p = m.end()
            return ("name", Name(re.sub(rb"#([0-9a-fA-F]{2})", lambda x: bytes([int(x.group(1), 16)]), m.group(1)).decode("latin-1")))
        if c == b"(":
            return ("str", self._literal())
        if d[p:p + 2] == b"<<":
            self.p += 2
            return ("<<", None)
        if d[p:p + 2] == b">>":
            self.p += 2
            return (">>", None)
        if c == b"<":
            e = d.find(b">", p)
            e = len(d) if e < 0 else e
            hx = re.sub(rb"[^0-9a-fA-F]", b"", d[p + 1:e])
            if len(hx) % 2:
                hx += b"0"
            self.p = e + 1
            return ("hex", bytes.fromhex(hx.decode()))
        if c in (b"[", b"]", b"{", b"}"):
            self.p += 1
            return (c.decode(), None)
        m = re.compile(rb"[^\s()<>\[\]{}/%]+").match(d, p)
        if not m:
            self.p += 1
            return self.token()
        self.p = m.end()
        w = m.group(0)
        if re.fullmatch(rb"[+-]?(\d+\.?\d*|\.\d+)", w):
            return ("num", float(w) if b"." in w else int(w))
        return ("kw", w.decode("latin-1"))

    def _literal(self):
        d, p, depth, out = self.d, self.p + 1, 1, bytearray()
        n = len(d)
        esc = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
        while p < n:
            c = d[p]
            if c == 0x5C:
                p += 1
                if p >= n:
                    break
                c = d[p]
                if c in esc:
                    out.append(esc[c])
                    p += 1
                elif 0x30 <= c <= 0x37:
                    m = re.compile(rb"[0-7]{1,3}").match(d, p)
                    out.append(int(m.group(0), 8) & 0xFF)
                    p = m.end()
                elif c == 0x0D:
                    p += 2 if d[p + 1:p + 2] == b"\n" else 1
                elif c == 0x0A:
                    p += 1
                else:
                    out.append(c)
                    p += 1
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    p += 1
                    break
            out.append(c)
            p += 1
        self.p = p
        return bytes(out)

    def obj(self, tok=None):
        """A whole object (number, name, string, array, dict, ref, kw)."""
        k, v = tok or self.token()
        if k == "num" and isinstance(v, int):
            save = self.p
            k2, v2 = self.token()
            if k2 == "num" and isinstance(v2, int):
                k3, v3 = self.token()
                if k3 == "kw" and v3 == "R":
                    return Ref(v, v2)
            self.p = save
            return v
        if k in ("num", "name"):
            return v
        if k in ("str", "hex"):
            return bytes(v)
        if k == "[":
            out = []
            while True:
                t = self.token()
                if t[0] in ("]", "eof"):
                    return out
                out.append(self.obj(t))
        if k == "<<":
            out = {}
            while True:
                t = self.token()
                if t[0] in (">>", "eof"):
                    return out
                if t[0] != "name":
                    continue
                out[t[1]] = self.obj()
        if k == "kw":
            return {"true": True, "false": False, "null": None}.get(v, ("kw", v))
        return None


OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


class Pdf:
    def __init__(self, data):
        if not data[:1024].lstrip().startswith(b"%PDF") and b"%PDF" not in data[:1024]:
            raise DocError("That isn't a PDF file.")
        self.d = data
        self.offsets = {}                 # num -> offset of "n g obj" (last one wins: incremental updates)
        for m in OBJ_RE.finditer(data):
            self.offsets[int(m.group(1))] = m.end()
        self.cache, self.objstm = {}, {}
        self.crypt = None
        self.trailer = self._trailer()
        self._index_objstreams()
        enc = self.trailer.get("Encrypt")
        if enc is not None:
            self.crypt = Crypt(self, self.get(enc), self.trailer.get("ID"))

    def _trailer(self):
        t = {}
        for m in re.finditer(rb"trailer\s*<<", self.d):
            lx = Lexer(self.d, m.end() - 2)
            d = lx.obj()
            if isinstance(d, dict):
                t.update({k: v for k, v in d.items() if k not in t or k in ("Root", "Encrypt", "ID")})
        for num, off in self.offsets.items():          # cross-reference streams carry the trailer keys too
            head = self.d[off:off + 400]
            if b"/XRef" in head:
                o = self._parse_at(off, num, decrypt=False)
                if isinstance(o, Stream) and o.d.get("Type") == "XRef":
                    for k in ("Root", "Encrypt", "ID", "Info"):
                        if k in o.d:
                            t[k] = o.d[k]
        return t

    def _index_objstreams(self):
        for num, off in list(self.offsets.items()):
            if b"/ObjStm" not in self.d[off:off + 300]:
                continue
            o = self.get(Ref(num, 0))
            if not isinstance(o, Stream) or o.d.get("Type") != "ObjStm":
                continue
            try:
                data = self.decode(o)
            except DocError:
                continue
            n, first = int(o.d.get("N", 0)), int(o.d.get("First", 0))
            lx = Lexer(data)
            nums = []
            for _ in range(n):
                a, b = lx.obj(), lx.obj()
                if isinstance(a, int) and isinstance(b, int):
                    nums.append((a, first + b))
            for a, at in nums:
                if a not in self.offsets:
                    self.objstm[a] = (data, at)

    def _parse_at(self, off, num, decrypt=True):
        lx = Lexer(self.d, off)
        o = lx.obj()
        if isinstance(o, dict):
            save = lx.p
            k, v = lx.token()
            if k == "kw" and v == "stream":
                p = lx.p
                if self.d[p:p + 2] == b"\r\n":
                    p += 2
                elif self.d[p:p + 1] in (b"\n", b"\r"):
                    p += 1
                ln = o.get("Length")
                if isinstance(ln, Ref):
                    ln = self.get(ln) if ln.num != num else None
                end = p + ln if isinstance(ln, int) and self.d[p + ln:p + ln + 30].lstrip().startswith(b"endstream") else None
                if end is None:
                    e = self.d.find(b"endstream", p)
                    end = len(self.d) if e < 0 else e
                    while end > p and self.d[end - 1:end] in (b"\n", b"\r"):
                        end -= 1
                raw = self.d[p:end]
                return Stream(o, raw, num if decrypt and o.get("Type") != "XRef" else None)
            lx.p = save
        if decrypt and self.crypt:
            o = self.crypt.strings(num, o)
        return o

    def get(self, o):
        """Resolve a reference (recursively, so a Ref to a Ref works)."""
        for _ in range(16):
            if not isinstance(o, Ref):
                return o
            num = o.num
            if num in self.cache:
                o = self.cache[num]
                continue
            self.cache[num] = None                     # guards against loops
            if num in self.offsets:
                val = self._parse_at(self.offsets[num], num)
            elif num in self.objstm:
                data, at = self.objstm[num]
                val = Lexer(data, at).obj()
            else:
                val = None
            self.cache[num] = val
            o = val
        return None

    def decode(self, s):
        data = s.raw
        if self.crypt and s.num is not None:          # decrypted only when needed, so pictures never are
            data = self.crypt.decrypt(s.num, data)
        filters = self.get(s.d.get("Filter"))
        parms = self.get(s.d.get("DecodeParms") or s.d.get("DP"))
        filters = filters if isinstance(filters, list) else [filters] if filters else []
        parms = parms if isinstance(parms, list) else [parms] * len(filters)
        for f, prm in zip(filters, parms):
            f, prm = self.get(f), self.get(prm) or {}
            if f in ("FlateDecode", "Fl"):
                data = _inflate(data)
                data = _predict(data, prm)
            elif f in ("LZWDecode", "LZW"):
                data = _predict(_lzw(data, int(prm.get("EarlyChange", 1))), prm)
            elif f in ("ASCII85Decode", "A85"):
                data = _a85(data)
            elif f in ("ASCIIHexDecode", "AHx"):
                data = bytes.fromhex(re.sub(rb"[^0-9a-fA-F]", b"", data.split(b">")[0]).decode() + ("0" if len(re.sub(rb"[^0-9a-fA-F]", b"", data.split(b">")[0])) % 2 else ""))
            elif f in ("RunLengthDecode", "RL"):
                data = _runlength(data)
            elif f == "Crypt":
                continue
            else:
                raise DocError("image data")          # DCT, JPX, CCITT, JBIG2: pictures, not text
        return data

    def pages(self):
        root = self.get(self.trailer.get("Root"))
        if not isinstance(root, dict):                 # damaged trailer: find the catalog by hand
            for num in self.offsets:
                o = self.get(Ref(num, 0))
                if isinstance(o, dict) and o.get("Type") == "Catalog":
                    root = o
                    break
        out, seen = [], set()

        def walk(node, inherited, depth=0):
            if depth > 40 or len(out) >= MAX_PAGES:
                return
            nd = self.get(node)
            if not isinstance(nd, dict) or (isinstance(node, Ref) and node.num in seen):
                return
            if isinstance(node, Ref):
                seen.add(node.num)
            inh = dict(inherited)
            if "Resources" in nd:
                inh["Resources"] = nd["Resources"]
            if nd.get("Type") == "Pages" or "Kids" in nd:
                for k in self.get(nd.get("Kids")) or []:
                    walk(k, inh, depth + 1)
            else:
                out.append((nd, self.get(inh.get("Resources")) or {}))
        if isinstance(root, dict):
            walk(root.get("Pages"), {})
        return out


def _inflate(data):
    try:
        return zlib.decompress(data)
    except zlib.error:
        d = zlib.decompressobj()                      # damaged or truncated: keep what decodes
        try:
            return d.decompress(data)
        except zlib.error:
            try:
                return zlib.decompressobj(-15).decompress(data[2:])
            except zlib.error:
                return b""


def _predict(data, prm):
    pred = int((prm or {}).get("Predictor", 1) or 1)
    if pred < 10:
        return data
    cols = int(prm.get("Columns", 1) or 1) * int(prm.get("Colors", 1) or 1) * int(prm.get("BitsPerComponent", 8) or 8) // 8
    bpp = max(1, int(prm.get("Colors", 1) or 1) * int(prm.get("BitsPerComponent", 8) or 8) // 8)
    out, prev = bytearray(), bytearray(cols)
    for i in range(0, len(data), cols + 1):
        ft, row = data[i], bytearray(data[i + 1:i + 1 + cols])
        for j in range(len(row)):
            a = row[j - bpp] if j >= bpp else 0
            b = prev[j] if j < len(prev) else 0
            c = prev[j - bpp] if j >= bpp and j - bpp < len(prev) else 0
            if ft == 1:
                row[j] = (row[j] + a) & 255
            elif ft == 2:
                row[j] = (row[j] + b) & 255
            elif ft == 3:
                row[j] = (row[j] + (a + b) // 2) & 255
            elif ft == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                row[j] = (row[j] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += row
        prev = row
    return bytes(out)


def _lzw(data, early=1):
    out, table = bytearray(), [bytes([i]) for i in range(256)] + [None, None]
    bits, buf, nbits, prev = 9, 0, 0, None
    for byte in data:
        buf, nbits = (buf << 8) | byte, nbits + 8
        while nbits >= bits:
            code = (buf >> (nbits - bits)) & ((1 << bits) - 1)
            nbits -= bits
            if code == 256:
                table, bits, prev = table[:258], 9, None
                continue
            if code == 257:
                return bytes(out)
            if code < len(table) and table[code] is not None:
                entry = table[code]
                if prev is not None:
                    table.append(prev + entry[:1])
            elif prev is not None:
                entry = prev + prev[:1]
                table.append(entry)
            else:
                continue
            out += entry
            prev = entry
            if len(table) + early >= (1 << bits) and bits < 12:
                bits += 1
    return bytes(out)


def _a85(data):
    data = re.sub(rb"\s", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    data = data.split(b"~>")[0]
    try:
        return base64.a85decode(data)
    except ValueError:
        return b""


def _runlength(data):
    out, i = bytearray(), 0
    while i < len(data):
        n = data[i]
        if n == 128:
            break
        if n < 128:
            out += data[i + 1:i + 2 + n]
            i += n + 2
        else:
            out += data[i + 1:i + 2] * (257 - n)
            i += 2
    return bytes(out)


# ── PDF: encryption (only files with an empty open password) ─────────────
PAD = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def _rc4(key, data):
    s, j = list(range(256)), 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(len(data)), 0, 0
    for k, c in enumerate(data):
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        out[k] = c ^ s[(s[i] + s[j]) & 255]
    return bytes(out)


class Crypt:
    def __init__(self, pdf, enc, ids):
        if not isinstance(enc, dict) or enc.get("Filter") != "Standard":
            raise DocError("That PDF is protected with a certificate or another security handler, so it can't be read here.")
        self.v, self.r = int(enc.get("V", 0)), int(enc.get("R", 2))
        o = pdf.get(enc.get("O")) or b""
        u = pdf.get(enc.get("U")) or b""
        p = int(enc.get("P", 0)) & 0xFFFFFFFF
        self.aes = False
        self.meta = enc.get("EncryptMetadata", True) is not False
        if self.v >= 4:
            cf = pdf.get(enc.get("CF")) or {}
            stmf = cf.get(enc.get("StmF", "Identity"), {}) if isinstance(cf, dict) else {}
            stmf = pdf.get(stmf) or {}
            self.aes = stmf.get("CFM") in ("AESV2", "AESV3")
        if self.r >= 5:                               # AES-256: hash algorithm 2.A / 2.B
            ue = pdf.get(enc.get("UE")) or b""
            if self._hash6(b"", u[32:40], b"") != u[:32]:
                raise DocError("That PDF needs a password to open.")
            key = self._hash6(b"", u[40:48], b"")
            self.key = _aes_cbc_decrypt(key, b"\0" * 16, ue, pad=False)[:32]
            self.aes = True
            return
        n = int(enc.get("Length", 40)) // 8 if self.v > 1 else 5
        first_id = (ids[0] if isinstance(ids, list) and ids else b"")
        first_id = pdf.get(first_id) or b""
        h = hashlib.md5(PAD + o + struct.pack("<I", p) + first_id)
        if self.r >= 4 and not self.meta:
            h.update(b"\xff\xff\xff\xff")
        key = h.digest()
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key[:n]).digest()
        self.key = key[:n]
        if self.r == 2:
            ok = _rc4(self.key, PAD) == u[:32]
        else:
            x = hashlib.md5(PAD + first_id).digest()
            x = _rc4(self.key, x)
            for i in range(1, 20):
                x = _rc4(bytes(b ^ i for b in self.key), x)
            ok = x == u[:16]
        if not ok:
            raise DocError("That PDF needs a password to open.")

    def _hash6(self, pw, salt, udata):
        k = hashlib.sha256(pw + salt + udata).digest()
        if self.r == 5:
            return k
        i = 0
        while True:
            k1 = (pw + k + udata) * 64
            e = _aes_cbc_encrypt(k[:16], k[16:32], k1)
            mod = sum(e[:16]) % 3
            k = (hashlib.sha256, hashlib.sha384, hashlib.sha512)[mod](e).digest()
            i += 1
            if i >= 64 and e[-1] <= i - 32:
                return k[:32]

    def _objkey(self, num, gen=0):
        if self.r >= 5:
            return self.key
        k = hashlib.md5(self.key + struct.pack("<I", num)[:3] + struct.pack("<I", gen)[:2] + (b"sAlT" if self.aes else b"")).digest()
        return k[:min(len(self.key) + 5, 16)]

    def decrypt(self, num, data):
        k = self._objkey(num)
        if self.aes:
            if len(data) < 32:
                return b""
            return _aes_cbc_decrypt(k, data[:16], data[16:])
        return _rc4(k, data)

    def strings(self, num, o):
        if isinstance(o, bytes):
            return self.decrypt(num, o)
        if isinstance(o, list):
            return [self.strings(num, x) for x in o]
        if isinstance(o, dict):
            return {k: self.strings(num, v) for k, v in o.items()}
        return o


# a small AES (decrypt for files; encrypt is only needed for the AES-256 password hash)
_SBOX = []
_INV = []


def _aes_tables():
    if _SBOX:
        return
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    _SBOX.extend(sbox)
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _INV.extend(inv)


def _xt(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


_M = {}


def _mtab(k):
    if k not in _M:
        _M[k] = [_mul(i, k) for i in range(256)]
    return _M[k]


def _expand(key):
    _aes_tables()
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xt(rcon)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([a ^ b for a, b in zip(w[i - nk], t)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(nr + 1)], nr


def _enc_block(rk, nr, b):
    S, m2, m3 = _SBOX, _mtab(2), _mtab(3)
    s = [x ^ k for x, k in zip(b, rk[0])]
    for r in range(1, nr + 1):
        t = [S[s[0]], S[s[5]], S[s[10]], S[s[15]], S[s[4]], S[s[9]], S[s[14]], S[s[3]],
             S[s[8]], S[s[13]], S[s[2]], S[s[7]], S[s[12]], S[s[1]], S[s[6]], S[s[11]]]
        if r != nr:
            m = []
            for c in (0, 4, 8, 12):
                a0, a1, a2, a3 = t[c], t[c + 1], t[c + 2], t[c + 3]
                m += (m2[a0] ^ m3[a1] ^ a2 ^ a3, a0 ^ m2[a1] ^ m3[a2] ^ a3, a0 ^ a1 ^ m2[a2] ^ m3[a3], m3[a0] ^ a1 ^ a2 ^ m2[a3])
            t = m
        k = rk[r]
        s = [t[i] ^ k[i] for i in range(16)]
    return bytes(s)


def _dec_block(rk, nr, b):
    I, m9, m11, m13, m14 = _INV, _mtab(9), _mtab(11), _mtab(13), _mtab(14)
    s = [x ^ k for x, k in zip(b, rk[nr])]
    for r in range(nr - 1, -1, -1):
        t = [I[s[0]], I[s[13]], I[s[10]], I[s[7]], I[s[4]], I[s[1]], I[s[14]], I[s[11]],
             I[s[8]], I[s[5]], I[s[2]], I[s[15]], I[s[12]], I[s[9]], I[s[6]], I[s[3]]]
        k = rk[r]
        t = [t[i] ^ k[i] for i in range(16)]
        if r != 0:
            m = []
            for c in (0, 4, 8, 12):
                a0, a1, a2, a3 = t[c], t[c + 1], t[c + 2], t[c + 3]
                m += (m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3], m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
                      m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3], m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3])
            t = m
        s = t
    return bytes(s)


def _aes_cbc_decrypt(key, iv, data, pad=True):
    rk, nr = _expand(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data) - len(data) % 16, 16):
        blk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_dec_block(rk, nr, blk), prev))
        prev = blk
    if pad and out and 1 <= out[-1] <= 16:
        out = out[:-out[-1]]
    return bytes(out)


def _aes_cbc_encrypt(key, iv, data):
    rk, nr = _expand(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        prev = _enc_block(rk, nr, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return bytes(out)


# ── PDF: fonts ───────────────────────────────────────────────────────────
def _enc_table(name):
    if name == "MacRomanEncoding":
        return {i: bytes([i]).decode("mac_roman") for i in range(32, 256)}
    t = {i: bytes([i]).decode("cp1252", "replace") for i in range(32, 256)}
    t.update({0x27: "'", 0x60: "`"}) if name == "StandardEncoding" else None
    return t


GLYPHS = {"space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%", "ampersand": "&",
          "quotesingle": "'", "quoteright": "’", "quoteleft": "‘", "parenleft": "(", "parenright": ")",
          "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "minus": "−", "period": ".", "slash": "/",
          "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
          "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^", "underscore": "_",
          "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~", "bullet": "•",
          "endash": "–", "emdash": "—", "quotedblleft": "“", "quotedblright": "”", "ellipsis": "…",
          "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl", "copyright": "©", "registered": "®",
          "trademark": "™", "degree": "°", "Euro": "€", "sterling": "£", "yen": "¥",
          "section": "§", "paragraph": "¶", "dagger": "†", "daggerdbl": "‡", "periodcentered": "·",
          "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
          "eight": "8", "nine": "9", "nbspace": " ", "germandbls": "ß", "eacute": "é", "egrave": "è",
          "aacute": "á", "agrave": "à", "udieresis": "ü", "odieresis": "ö", "adieresis": "ä",
          "ccedilla": "ç", "ntilde": "ñ", "oacute": "ó", "iacute": "í", "uacute": "ú"}


def _glyph(name):
    if name in GLYPHS:
        return GLYPHS[name]
    if len(name) == 1:
        return name
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4,})", name) or re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return "".join(chr(int(m.group(1)[i:i + 4], 16)) for i in range(0, len(m.group(1)), 4))
        except ValueError:
            return ""
    base = name.split(".")[0].split("_")[0]
    return GLYPHS.get(base, base if len(base) == 1 else "")


class Font:
    def __init__(self, pdf, fd):
        fd = pdf.get(fd) or {}
        self.two = fd.get("Subtype") == "Type0"
        self.map, self.ranges = {}, []
        self.table = None
        tu = pdf.get(fd.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                self._cmap(pdf.decode(tu))
            except DocError:
                pass
        if not self.map and not self.two:
            enc = pdf.get(fd.get("Encoding"))
            if isinstance(enc, dict):
                self.table = _enc_table(enc.get("BaseEncoding") or "WinAnsiEncoding")
                code = 0
                for x in pdf.get(enc.get("Differences")) or []:
                    x = pdf.get(x)
                    if isinstance(x, int):
                        code = x
                    elif isinstance(x, str):
                        self.table[code] = _glyph(x)
                        code += 1
            else:
                self.table = _enc_table(enc if isinstance(enc, str) else "WinAnsiEncoding")
        # glyph widths (1/1000 em), to tell a gap between words from letters set one by one
        self.w, self.dw = {}, 500
        if self.two:
            desc = pdf.get((pdf.get(fd.get("DescendantFonts")) or [None])[0]) or {}
            self.dw = pdf.get(desc.get("DW")) or 1000
            w = pdf.get(desc.get("W")) or []
            i = 0
            while i < len(w):
                a = pdf.get(w[i])
                b = pdf.get(w[i + 1]) if i + 1 < len(w) else None
                if isinstance(b, list):
                    for j, x in enumerate(b):
                        x = pdf.get(x)
                        if isinstance(x, (int, float)):
                            self.w[a + j] = x
                    i += 2
                elif i + 2 < len(w):
                    c = pdf.get(w[i + 2])
                    if isinstance(a, int) and isinstance(b, int) and isinstance(c, (int, float)) and b - a < 65536:
                        for code in range(a, b + 1):
                            self.w[code] = c
                    i += 3
                else:
                    break
        else:
            first = pdf.get(fd.get("FirstChar")) or 0
            for j, x in enumerate(pdf.get(fd.get("Widths")) or []):
                x = pdf.get(x)
                if isinstance(x, (int, float)):
                    self.w[first + j] = x
            fdesc = pdf.get(fd.get("FontDescriptor")) or {}
            self.dw = pdf.get(fdesc.get("MissingWidth")) or 500 if isinstance(fdesc, dict) else 500

    def _cmap(self, data):
        lx = Lexer(data)
        stack = []
        while True:
            k, v = lx.token()
            if k == "eof":
                break
            if k in ("hex", "str", "num"):
                stack.append(v)
            elif k == "[":
                arr = []
                while True:
                    t = lx.token()
                    if t[0] in ("]", "eof"):
                        break
                    arr.append(t[1])
                stack.append(arr)
            elif k == "kw" and v == "endbfchar":
                for i in range(0, len(stack) - 1, 2):
                    src, dst = stack[i], stack[i + 1]
                    if isinstance(src, bytes) and isinstance(dst, bytes):
                        self.map[src] = _utf16(dst)
                stack = []
            elif k == "kw" and v == "endbfrange":
                for i in range(0, len(stack) - 2, 3):
                    lo, hi, dst = stack[i], stack[i + 1], stack[i + 2]
                    if not (isinstance(lo, bytes) and isinstance(hi, bytes)):
                        continue
                    a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
                    if b - a > 65535:
                        continue
                    if isinstance(dst, list):
                        for j, d in enumerate(dst[:b - a + 1]):
                            if isinstance(d, bytes):
                                self.map[(a + j).to_bytes(len(lo), "big")] = _utf16(d)
                    elif isinstance(dst, bytes):
                        base = int.from_bytes(dst, "big")
                        for j in range(b - a + 1):
                            self.map[(a + j).to_bytes(len(lo), "big")] = _utf16((base + j).to_bytes(max(len(dst), 2), "big"))
                stack = []
            elif k == "kw" and v in ("begincodespacerange",):
                stack = []
            elif k == "kw" and v == "endcodespacerange":
                for i in range(0, len(stack) - 1, 2):
                    if isinstance(stack[i], bytes):
                        self.ranges.append(len(stack[i]))
                stack = []
            elif k == "kw" and v in ("beginbfchar", "beginbfrange"):
                stack = []
        if self.map and not self.ranges:
            self.ranges = sorted({len(k) for k in self.map})

    def glyphs(self, s):
        """(text, width in 1/1000 em, is the space byte) for each character code in s."""
        lens = sorted(set(self.ranges or [1, 2])) if self.map else [2 if self.two else 1]
        i = 0
        while i < len(s):
            u, n = None, None
            if self.map:
                for ln in lens:
                    ch = s[i:i + ln]
                    if ch in self.map:
                        u, n = self.map[ch], ln
                        break
            if n is None:
                n = 2 if self.two else 1
                b = s[i]
                u = "" if self.two else (self.table.get(b, "") if self.table else (chr(b) if 32 <= b < 127 else "")) if b >= 32 else ""
            code = int.from_bytes(s[i:i + n], "big")
            yield u, self.w.get(code, self.dw), n == 1 and s[i] == 32
            i += n

    def text(self, s):
        if self.map:
            out, i = [], 0
            lens = sorted(set(self.ranges or [1, 2]))
            while i < len(s):
                for n in lens:
                    ch = s[i:i + n]
                    if ch in self.map:
                        out.append(self.map[ch])
                        i += n
                        break
                else:
                    n = 2 if self.two else 1
                    if not self.two and 32 <= s[i] < 127:
                        out.append(chr(s[i]))
                    i += n
            return "".join(out)
        if self.two:
            return ""                                  # CID font with no Unicode map: can't know the letters
        return "".join(self.table.get(b, "") if b >= 32 else "" for b in s)


def _utf16(b):
    try:
        return b.decode("utf-16-be") if len(b) >= 2 else b.decode("latin-1")
    except UnicodeDecodeError:
        return ""


# ── PDF: page text ───────────────────────────────────────────────────────
def _mm(m, n):
    """m × n for PDF matrices (a, b, c, d, e, f)."""
    return (m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3], m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3],
            m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5])


IDENT = (1, 0, 0, 1, 0, 0)
PUA_BULLETS = {0xF0B7: "\u2022", 0xF0A7: "\u25aa", 0xF0D8: "\u27a2", 0xF076: "\u2756", 0xF0FC: "\u2713", 0xF0A8: "\u25a1",
               0xF06E: "\u25a0", 0xF0E0: "\u2192", 0xF0D9: "\u2192", 0xF02D: "-"}


class _Page:
    """Runs a page's content stream, keeping where each piece of text lands, so words, lines and table rows come
    out the way they look: a small gap is a space, a big one on the same line separates columns, a drop is a new
    line (a bigger drop, a new paragraph)."""

    def __init__(self, pdf):
        self.pdf, self.out, self.fonts = pdf, [], {}
        self.images = 0
        self.last = None                  # (x end, y, size) of the text placed last

    def run(self, data, resources, ctm=IDENT, depth=0):
        if depth > 8:
            return
        res = self.pdf.get(resources) or {}
        fonts = self.pdf.get(res.get("Font")) or {}
        xobj = self.pdf.get(res.get("XObject")) or {}
        lx = Lexer(data)
        ops, stack = [], []
        font, tfs, tc, tw, th, tl = None, 10.0, 0.0, 0.0, 1.0, 0.0
        tm = tlm = IDENT
        nums = lambda n: len(ops) >= n and all(isinstance(x, (int, float)) for x in ops[-n:])

        def show(sb):
            nonlocal tm
            if not font:
                return
            for u, w, sp in font.glyphs(sb):
                m = _mm(tm, ctm)
                size = abs(tfs) * abs(m[0] * m[3] - m[1] * m[2]) ** 0.5       # the text's height on the page
                if u:
                    self.place(m[4], m[5], size or 1, u)
                adv = (w / 1000.0 * tfs + tc + (tw if sp else 0)) * th
                tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv * tm[0], tm[5] + adv * tm[1])
                if u:
                    m = _mm(tm, ctm)
                    self.last = (m[4], m[5], size or 1)

        while True:
            k, v = lx.token()
            if k == "eof":
                break
            if k == "kw":
                if v == "BI":                          # inline image: skip to EI
                    e = data.find(b"EI", lx.p)
                    lx.p = len(data) if e < 0 else e + 2
                    self.images += 1
                elif v == "q":
                    stack.append(ctm)
                elif v == "Q":
                    ctm = stack.pop() if stack else ctm
                elif v == "cm" and nums(6):
                    ctm = _mm(tuple(ops[-6:]), ctm)
                elif v == "BT":
                    tm = tlm = IDENT
                elif v == "Tf" and len(ops) >= 2:
                    font = self._font(fonts, ops[-2])
                    tfs = ops[-1] if isinstance(ops[-1], (int, float)) else tfs
                elif v == "Tc" and nums(1):
                    tc = ops[-1]
                elif v == "Tw" and nums(1):
                    tw = ops[-1]
                elif v == "Tz" and nums(1):
                    th = ops[-1] / 100.0
                elif v == "TL" and nums(1):
                    tl = ops[-1]
                elif v in ("Td", "TD") and nums(2):
                    tx, ty = ops[-2], ops[-1]
                    tlm = (tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] + tx * tlm[0] + ty * tlm[2], tlm[5] + tx * tlm[1] + ty * tlm[3])
                    tm = tlm
                    if v == "TD":
                        tl = -ty
                elif v == "Tm" and nums(6):
                    tm = tlm = tuple(ops[-6:])
                elif v in ("T*", "'", '"'):
                    if v == '"' and len(ops) >= 3 and isinstance(ops[-3], (int, float)):
                        tw, tc = ops[-3], ops[-2] if isinstance(ops[-2], (int, float)) else tc
                    tlm = (tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] - tl * tlm[2], tlm[5] - tl * tlm[3])
                    tm = tlm
                    if v != "T*" and ops and isinstance(ops[-1], bytes):
                        show(ops[-1])
                elif v == "Tj" and ops and isinstance(ops[-1], bytes):
                    show(ops[-1])
                elif v == "TJ" and ops and isinstance(ops[-1], list):
                    for x in ops[-1]:
                        if isinstance(x, bytes):
                            show(x)
                        elif isinstance(x, (int, float)):
                            adv = -x / 1000.0 * tfs * th
                            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv * tm[0], tm[5] + adv * tm[1])
                elif v == "Do" and ops and isinstance(ops[-1], str):
                    xo = self.pdf.get(xobj.get(ops[-1]))
                    if isinstance(xo, Stream):
                        st = xo.d.get("Subtype")
                        if st == "Form":
                            mat = self.pdf.get(xo.d.get("Matrix"))
                            fm = tuple(mat) if isinstance(mat, list) and len(mat) == 6 and all(isinstance(x, (int, float)) for x in mat) else IDENT
                            try:
                                self.run(self.pdf.decode(xo), xo.d.get("Resources") or res, _mm(fm, ctm), depth + 1)
                            except DocError:
                                pass
                        elif st == "Image":
                            self.images += 1
                ops = []
            elif k == "[":
                arr = []
                while True:
                    t = lx.token()
                    if t[0] in ("]", "eof"):
                        break
                    if t[0] in ("str", "hex", "num"):
                        arr.append(bytes(t[1]) if t[0] != "num" else t[1])
                ops.append(arr)
            elif k == "<<":
                lx.p -= 2
                lx.obj()
            elif k in ("str", "hex"):
                ops.append(bytes(v))
            elif k in ("num", "name"):
                ops.append(v)

    def place(self, x, y, size, u):
        if self.last is not None:
            lx, ly, ls = self.last
            unit = max(size, ls, 0.1)
            dy = ly - y
            if abs(dy) > 0.5 * unit:                   # another line
                self.nl()
                if dy > 2.0 * unit:                    # a bigger drop: a new paragraph
                    self.out.append("\n")
            else:
                dx = x - lx
                if dx > 2.0 * unit:                    # far along the same line: the next column
                    self.gap("   ")
                elif dx > 0.15 * unit or dx < -0.5 * unit:
                    self.gap(" ")
        self.out.append(u)

    def _font(self, fonts, name):
        if name not in self.fonts:
            self.fonts[name] = Font(self.pdf, fonts.get(name))
        return self.fonts[name]

    def gap(self, g):
        if self.out and not self.out[-1].endswith((" ", "\n")) and not g.startswith(self.out[-1][-1:] or "\0"):
            self.out.append(g)

    def nl(self):
        if self.out and not self.out[-1].endswith("\n"):
            while self.out and self.out[-1].strip(" ") == "":
                self.out.pop()
            self.out.append("\n")

    def text(self):
        t = "".join(self.out)
        # Symbol/Wingdings bullets live in the private-use area: show them as bullets, drop other such glyphs
        t = t.translate(PUA_BULLETS)
        t = re.sub("[\ue000-\uf8ff]", "", t)
        t = re.sub(r"[\t\u00a0]+", " ", t)
        t = re.sub(r"(?<=\S) {4,}", "   ", t)
        t = re.sub(r" +\n", "\n", t)
        t = re.sub(r"\n +", "\n", t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()


def pdf_text(data):
    """(text, info) from a PDF's bytes. Pages are marked "[Page N]"."""
    pdf = Pdf(data)
    pages = pdf.pages()
    if not pages:
        raise DocError("No pages were found in that PDF; it may be damaged.")
    out, scanned, empty = [], [], 0
    for i, (pg, res) in enumerate(pages, 1):
        contents = pdf.get(pg.get("Contents"))
        parts = contents if isinstance(contents, list) else [contents] if contents is not None else []
        data_ = b""
        for c in parts:
            c = pdf.get(c)
            if isinstance(c, Stream):
                try:
                    data_ += pdf.decode(c) + b"\n"
                except DocError:
                    pass
        page = _Page(pdf)
        try:
            page.run(data_, res)
        except (RecursionError, ValueError, IndexError, struct.error):
            pass
        t = page.text()
        if not t:
            empty += 1
            if page.images:
                scanned.append(i)
        out.append("[Page %d]\n%s" % (i, t or ("(this page is a picture with no text: it was scanned)" if page.images else "(no text on this page)")))
    info = {"kind": "pdf", "pages": len(pages), "scanned_pages": scanned}
    if empty == len(pages):
        if scanned:
            raise DocError("That PDF is scanned: its pages are pictures with no text in them, so the text can't be read "
                           "without OCR. Open it in Adobe Acrobat or Word and save it with recognized text, then try again.")
        raise DocError("No text was found in that PDF.")
    text = "\n\n".join(out)
    if scanned:
        text = ("(Note: %d of %d pages are scanned pictures with no text, so they're missing below.)\n\n" % (len(scanned), len(pages))) + text
    return text + "\n", info
