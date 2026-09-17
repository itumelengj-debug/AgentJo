"""Read uploaded files into text for the chat — locally and dependency-light.

Plain-text and code files are read directly. Office files (.docx, .pptx, .xlsx)
are parsed from their XML with the standard library, so they need no extra
package. PDFs use pypdf if it is installed; spreadsheets prefer openpyxl when
present. Anything missing degrades to a short, clear note rather than an error.

Nothing here reaches the network: extraction is entirely local.
"""

import os
import re
import zipfile
import xml.etree.ElementTree as ET

from . import config

# Per-file character cap so a huge document can't blow the context window.
MAX_CHARS = getattr(config, "ATTACH_MAX_CHARS", 60_000)
_MAX_ROWS = 5000          # spreadsheet rows scanned per sheet
_MAX_PAGES = 300          # PDF pages scanned

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".css", ".js", ".jsx", ".ts",
    ".tsx", ".py", ".rb", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".cs", ".php", ".sh", ".bash", ".zsh", ".sql", ".toml", ".ini",
    ".cfg", ".conf", ".env", ".tex", ".r", ".jl", ".kt", ".swift", ".scala",
    ".pl", ".lua", ".dart", ".vue", ".svelte", ".gradle", ".properties",
    ".bat", ".ps1", ".dockerfile", ".makefile", ".gitignore", ".m", ".f90",
}

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAW = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ---------------------------------------------------------------------- #
# Public entry point
# ---------------------------------------------------------------------- #
def extract_text(path: str):
    """Return (text, note). `note` is a short, user-facing status string."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    try:
        if ext == ".pdf":
            text, note = _pdf(path)
        elif ext == ".docx":
            text, note = _docx(path)
        elif ext == ".pptx":
            text, note = _pptx(path)
        elif ext in (".xlsx", ".xlsm"):
            text, note = _xlsx(path)
        elif ext in (".odt", ".ods", ".odp"):
            text, note = _odf(path)
        elif ext in (".html", ".htm", ".xhtml"):
            text, note = _html(path)
        elif ext == ".rtf":
            text, note = _rtf(path)
        elif ext in (".eml", ".msg"):
            text, note = _email(path)
        elif ext == ".epub":
            text, note = _epub(path)
        elif ext in (".zip",):
            text, note = _zip_listing(path)
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                     ".tif", ".tiff"):
            # an image attached to the CHAT goes to the model as an image;
            # this path is for one arriving as a document, where text is the
            # only thing a text model can use
            text, note = _image_text(path)
        elif ext in TEXT_EXTS or _looks_textual(path):
            text, note = _plain(path)
        else:
            return "", (f"{name}: I can't read {ext or 'a file with no '
                        'extension'} yet. Text, PDF, Word, Excel, PowerPoint, "
                        f"OpenDocument, HTML, RTF, email and EPUB all work.")
    except Exception as exc:
        return "", f"{name}: could not read ({type(exc).__name__})"

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        note = (note + "; truncated") if note else "truncated"
    return text, (note or f"read {name}")


# ---------------------------------------------------------------------- #
# Per-type readers
# ---------------------------------------------------------------------- #
def _plain(path):
    with open(path, "rb") as fh:
        raw = fh.read(MAX_CHARS * 4)
    return raw.decode("utf-8", "replace"), None


def _looks_textual(path):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:                       # null byte => binary
        return False
    printable = sum(1 for b in chunk if b in (9, 10, 13) or 32 <= b <= 126 or b >= 128)
    return printable / len(chunk) > 0.85


def _docx(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for para in root.iter(_WORD + "p"):
        parts = []
        for node in para.iter():
            if node.tag == _WORD + "t":
                parts.append(node.text or "")
            elif node.tag == _WORD + "tab":
                parts.append("\t")
            elif node.tag in (_WORD + "br", _WORD + "cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines).strip(), None


def _pptx(path):
    out = []
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist()
             if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for i, n in enumerate(slides, 1):
            root = ET.fromstring(z.read(n))
            texts = [t.text or "" for t in root.iter(_DRAW + "t")]
            body = " ".join(s for s in texts if s.strip())
            if body:
                out.append(f"[Slide {i}] {body}")
    return "\n\n".join(out).strip(), None


def _xlsx(path):
    try:
        import openpyxl
    except Exception:
        return _xlsx_stdlib(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= _MAX_ROWS:
                    break
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
            if rows:
                out.append(f"[{ws.title}]\n" + "\n".join(rows))
    finally:
        wb.close()
    return "\n\n".join(out).strip(), None


def _xlsx_stdlib(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.iter(_SHEET + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_SHEET + "t")))
        sheets = sorted(
            (n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        out = []
        for idx, sf in enumerate(sheets, 1):
            root = ET.fromstring(z.read(sf))
            rows = []
            for rn, row in enumerate(root.iter(_SHEET + "row")):
                if rn >= _MAX_ROWS:
                    break
                cells = []
                for c in row.iter(_SHEET + "c"):
                    t = c.get("t")
                    v = c.find(_SHEET + "v")
                    if t == "s" and v is not None:
                        try:
                            cells.append(shared[int(v.text)])
                        except (ValueError, IndexError):
                            cells.append("")
                    elif t == "inlineStr":
                        is_ = c.find(_SHEET + "is")
                        cells.append("".join(x.text or "" for x in is_.iter(_SHEET + "t"))
                                     if is_ is not None else "")
                    elif v is not None:
                        cells.append(v.text or "")
                    else:
                        cells.append("")
                if any(cell.strip() for cell in cells):
                    rows.append("\t".join(cells))
            if rows:
                out.append(f"[Sheet {idx}]\n" + "\n".join(rows))
    return "\n\n".join(out).strip(), None


def _pdf(path):
    try:
        from pypdf import PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader
        except Exception:
            return "", "PDF support needs pypdf (run: pip install pypdf)"
    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages, 1):
        if i > _MAX_PAGES:
            break
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt.strip():
            pages.append(txt.strip())
    text = "\n\n".join(pages).strip()
    if not text:
        return "", "no extractable text (the PDF may be scanned images)"
    return text, None


def _odf(path):
    """OpenDocument — what LibreOffice saves, and what a lot of South African
    government and NGO documents arrive as."""
    from odf import opendocument, text as odftext, teletype
    doc = opendocument.load(path)
    paras = doc.getElementsByType(odftext.P)
    body = "\n".join(teletype.extractText(p) for p in paras)
    return body, f"read {len(paras)} paragraph(s)"


def _html(path):
    from bs4 import BeautifulSoup
    raw = open(path, "rb").read().decode("utf-8", "replace")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    body = "\n".join(line.strip() for line in soup.get_text("\n").splitlines()
                     if line.strip())
    return body, "read the page text"


def _rtf(path):
    raw = open(path, "rb").read().decode("utf-8", "replace")
    try:
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(raw), "read as rich text"
    except ImportError:
        # a serviceable fallback beats refusing the file: strip the control
        # words and keep what is left
        import re as _re
        body = _re.sub(r"\\[a-z]+-?\d* ?", " ", raw)
        body = _re.sub(r"[{}]", " ", body)
        return " ".join(body.split()), ("read without striprtf — install it "
                                        "for cleaner output")


def _email(path):
    """A saved email, with its headers — which are usually the point."""
    import email
    from email import policy
    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    head = "\n".join(f"{k}: {msg.get(k, '')}" for k in
                      ("From", "To", "Cc", "Date", "Subject") if msg.get(k))
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not body:
                body = part.get_content()
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    from bs4 import BeautifulSoup
                    body = BeautifulSoup(part.get_content(),
                                         "html.parser").get_text("\n")
                    break
    else:
        body = str(msg.get_content())
    atts = [p.get_filename() for p in (msg.iter_attachments()
                                       if hasattr(msg, "iter_attachments")
                                       else []) if p.get_filename()]
    extra = f"\n\nAttachments: {', '.join(atts)}" if atts else ""
    return f"{head}\n\n{body}{extra}", "read the message and its headers"


def _epub(path):
    import zipfile
    from bs4 import BeautifulSoup
    parts = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(z.namelist()):
            if n.lower().endswith((".xhtml", ".html", ".htm")):
                soup = BeautifulSoup(z.read(n).decode("utf-8", "replace"),
                                     "html.parser")
                parts.append(soup.get_text("\n"))
    body = "\n\n".join(p.strip() for p in parts if p.strip())
    return body, f"read {len(parts)} section(s)"


def _zip_listing(path):
    """Not the contents — the manifest. Unpacking an archive someone sent you
    is a decision, not something to do silently on upload."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        names = z.namelist()[:400]
    listing = "\n".join(names)
    return (f"Archive containing {len(names)} file(s):\n{listing}",
            "listed the contents — ask me to extract if you want them read")


def _image_text(path):
    """Text inside an image, when one is attached as a document.

    OCR is genuinely unreliable on photographs, so this says what it did
    rather than presenting a guess as the file's contents."""
    try:
        from PIL import Image
        import pytesseract
        txt = pytesseract.image_to_string(Image.open(path)).strip()
        if txt:
            return txt, "read the text in the image (OCR — check it)"
        return "", "no readable text found in the image"
    except ImportError:
        return "", ("an image: attach it to a chat message and the model can "
                    "look at it directly, which beats OCR")
    except Exception as exc:
        return "", f"could not OCR the image ({type(exc).__name__})"
