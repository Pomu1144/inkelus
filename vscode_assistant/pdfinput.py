"""Bounded visual PDF reading for local vision models; original files stay intact."""
import base64
import threading
from collections import OrderedDict

RENDER_LOCK = threading.RLock()  # PyMuPDF calls must not run concurrently across chat workers

TOOLS = [{"type": "function", "function": {
    "name": "read_pdf_pages", "description": "Inspect original PDF pages visually, preserving tables, equations, diagrams and columns. Page numbers start at 1.",
    "parameters": {"type": "object", "properties": {
        "document": {"type": "string", "description": "Exact attached document name from the PDF inventory"},
        "pages": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 4}},
        "required": ["document", "pages"], "additionalProperties": False}}}]
TOOL_NAMES = {"read_pdf_pages"}


class Reader:
    def __init__(self, paths):
        try:
            import pymupdf
        except ImportError as exc:
            raise RuntimeError('Visual PDF reading needs PyMuPDF. Run: python -m pip install -r requirements-pdf.txt') from exc
        self.pdf = pymupdf
        self.paths = paths
        self.counts = {}
        self.loaded = OrderedDict()
        self.seen = set()
        for name, path in paths.items():
            with RENDER_LOCK, pymupdf.open(path) as doc:
                if doc.needs_pass:
                    raise ValueError('%s is password protected. Upload an unlocked copy.' % name)
                if not len(doc):
                    raise ValueError('%s has no PDF pages.' % name)
                self.counts[name] = len(doc)
        for name in list(paths)[:4]:
            self.load(name, [1])

    def load(self, name, pages):
        if name not in self.paths:
            raise ValueError('Choose an attached PDF from the inventory.')
        if not isinstance(pages, list) or not 1 <= len(pages) <= 4 or any(
                not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= self.counts[name] for p in pages):
            raise ValueError('Choose 1 to 4 valid page numbers (starting at 1).')
        with RENDER_LOCK, self.pdf.open(self.paths[name]) as doc:
            for number in dict.fromkeys(pages):
                key = (name, number)
                if key not in self.loaded:
                    page = doc[number - 1]
                    # Bound image memory even for unusually large page dimensions.
                    scale = min(2, 1800 / max(page.rect.width, page.rect.height, 1))
                    pix = page.get_pixmap(matrix=self.pdf.Matrix(scale, scale), colorspace=self.pdf.csRGB, alpha=False)
                    self.loaded[key] = base64.b64encode(pix.tobytes('png')).decode('ascii')
                self.loaded.move_to_end(key)
                self.seen.add(key)
                while len(self.loaded) > 8:
                    self.loaded.popitem(last=False)
        return 'Loaded original PDF page images: %s, pages %s. Images accompany this tool result in the next model request.' % (name, pages)

    def prompt(self):
        return ('PDF layout mode: original pages are supplied as images. Only the listed image pages are currently visible. '
                'Use read_pdf_pages to inspect additional relevant pages before answering; do not claim to have read unseen pages. '
                'For a whole-document task, inspect every page in batches. Never treat a text cache as the visual layout.\nPDF inventory:\n' +
                '\n'.join('%s: %d pages' % item for item in self.counts.items()))

    def message(self):
        return {'role': 'user', 'content': 'Reference PDF page images (image order):\n' +
                '\n'.join('%s — page %d' % key for key in self.loaded) +
                '\nUse these as reference for my latest request above.', 'images': list(self.loaded.values())}
