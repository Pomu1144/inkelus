"""Local document output. The model supplies text; Python creates the actual file."""
import io
import importlib.util
import os
import re
import tempfile
import uuid
from xml.sax.saxutils import escape

SYSTEM_PROMPT = """You can create downloadable PDF files with create_pdf, even with Web and Browser off.
When the user asks for a PDF, call create_pdf with the complete document content.
Use headings, paragraphs and lists. Do not return Python code or invent download links.
The app displays the file automatically after the tool succeeds. For a revision, create a new PDF
with the requested changes. This tool creates text documents, not edits to an existing PDF's layout.
Use readable plain-text equations; LaTeX typesetting and images are not supported by this tool."""
TOOLS = [{"type": "function", "function": {
    "name": "create_pdf", "description": "Create a real downloadable PDF from Markdown text in this conversation.",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string", "description": "Document title"},
        "content": {"type": "string", "description": "Complete document text with optional Markdown headings and lists"},
        "filename": {"type": "string", "description": "Suggested .pdf download name"}},
        "required": ["title", "content"], "additionalProperties": False}}}]
TOOL_NAMES = {"create_pdf"}


def available():
    return importlib.util.find_spec('reportlab') is not None


def filename(value):
    stem = re.sub(r'[^\w .-]', '_', str(value or 'document'), flags=re.UNICODE)
    stem = re.sub(r'\.pdf$', '', stem, flags=re.I).strip(' .')[:80] or 'document'
    return stem + '.pdf'


def render_pdf(title, content):
    """Render without executing model code or interpreting model-supplied HTML."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError('The PDF needs document text.')
    if not isinstance(title, str) or len(title) > 300 or len(content) > 200000:
        raise ValueError('Use a title under 300 characters and document under 200,000 characters.')
    try:
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.enums import TA_LEFT
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except ImportError as exc:
        raise RuntimeError('PDF output requires ReportLab. Install it with: python -m pip install reportlab') from exc
    styles = getSampleStyleSheet()
    for name in ('Normal', 'BodyText'):
        styles[name].fontSize = 11
        styles[name].leading = 16
        styles[name].alignment = TA_LEFT
    # ReportLab's bundled Vera fonts keep common Unicode symbols searchable.
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import reportlab
    fontdir = os.path.join(os.path.dirname(reportlab.__file__), 'fonts')
    for name, file in (('Vera', 'Vera.ttf'), ('Vera-Bold', 'VeraBd.ttf')):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, os.path.join(fontdir, file)))
    for name in ('Normal', 'BodyText', 'Title', 'Heading1', 'Heading2', 'Heading3'):
        styles[name].fontName = 'Vera-Bold' if name.startswith('Heading') or name == 'Title' else 'Vera'
        styles[name].textColor = colors.HexColor('#172033')
    # Reject unsupported glyphs rather than silently emitting blank boxes.
    supported = pdfmetrics.getFont('Vera').face.charToGlyph
    missing = sorted({c for c in title + content if not c.isspace() and ord(c) not in supported})
    if missing:
        raise ValueError('PDF font does not support these characters: ' + ' '.join(missing[:20]) +
                         '. Use plain-text equivalents (for example alpha, integral, or x^2).')
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, title=title, pagesize=(612, 792),
                            leftMargin=54, rightMargin=54, topMargin=50, bottomMargin=50)
    story = [Paragraph(escape(title), styles['Title']), Spacer(1, 12)]
    paragraph = []

    def flush():
        if paragraph:
            story.append(Paragraph('<br/>'.join(escape(s) for s in paragraph), styles['BodyText']))
            story.append(Spacer(1, 8))
            paragraph.clear()

    for line in content.splitlines():
        heading = re.match(r'^(#{1,3})\s+(.+)$', line)
        if heading:
            flush()
            story.append(Paragraph(escape(heading[2]), styles['Heading' + str(len(heading[1]))]))
        elif not line.strip():
            flush()
        elif re.match(r'^\s*([-*+] |\d+[.)] )', line):
            flush()
            story.append(Paragraph(escape(line.strip()), styles['BodyText']))
        else:
            paragraph.append(line)
    flush()

    def footer(canvas, document):
        canvas.setFont('Vera', 8)
        canvas.setFillColor(colors.HexColor('#64748b'))
        canvas.drawRightString(558, 28, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


def artifact_path(data_dir, chat_id, artifact_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(chat_id)) or not re.fullmatch(r'[a-f0-9]{32}', str(artifact_id)):
        raise ValueError('Invalid document ID.')
    return os.path.join(data_dir, 'artifacts', chat_id, artifact_id + '.pdf')


def create(data_dir, chat_id, title, content, suggested_name=None):
    data = render_pdf(title, content)
    artifact_id = uuid.uuid4().hex
    path = artifact_path(data_dir, chat_id, artifact_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'id': artifact_id, 'filename': filename(suggested_name or title),
            'title': title, 'mime': 'application/pdf', 'size': len(data)}
