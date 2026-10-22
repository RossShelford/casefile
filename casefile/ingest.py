"""Find files, pull plain text out of them, and cut the text into model-sized chunks.

Every chunk remembers where it came from so any extracted fact can be cited back to
a file and a page (PDF) or line (everything else).
"""

from __future__ import annotations

import bisect
import email
import email.policy
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".rtf"}
HTML_EXTS = {".html", ".htm"}
SUPPORTED_EXTS = TEXT_EXTS | HTML_EXTS | {".pdf", ".docx", ".eml"}


@dataclass
class Document:
    path: str  # relative to the scanned folder
    text: str = ""
    page_starts: list[int] | None = None  # PDF only: text offset where each page begins
    status: str = "ok"
    _line_starts: list[int] = field(default_factory=list, repr=False)

    def location(self, offset: int) -> str:
        """Human citation for a character offset: 'p. 4' for PDFs, 'line 120' otherwise."""
        if self.page_starts is not None:
            return f"p. {bisect.bisect_right(self.page_starts, offset)}"
        if not self._line_starts:
            self._line_starts = [0] + [i + 1 for i, ch in enumerate(self.text) if ch == "\n"]
        return f"line {bisect.bisect_right(self._line_starts, offset)}"


@dataclass
class Chunk:
    doc_index: int
    index: int  # position within its document
    start: int  # offset into Document.text
    text: str


def scan_folder(root: Path) -> list[Document]:
    """Read every file under root (skipping hidden files/dirs), in stable path order."""
    docs = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        docs.append(read_document(path, rel.as_posix()))
    return docs


def read_document(path: Path, rel: str) -> Document:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return Document(rel, status="skipped: unsupported file type")
    try:
        if ext == ".pdf":
            doc = _read_pdf(path, rel)
        elif ext == ".docx":
            doc = Document(rel, _read_docx(path))
        elif ext == ".eml":
            doc = Document(rel, _read_eml(path))
        elif ext in HTML_EXTS:
            doc = Document(rel, _html_to_text(_decode(path.read_bytes())))
        else:
            doc = Document(rel, _decode(path.read_bytes()))
    except Exception as exc:  # one unreadable file must not stop a scan of thousands
        return Document(rel, status=f"error: {type(exc).__name__}: {exc}")
    if not doc.text.strip():
        doc.status = "no text found (scanned image? needs OCR)" if ext == ".pdf" else "empty"
    return doc


def chunk_document(doc: Document, doc_index: int, size: int, overlap: int) -> list[Chunk]:
    """Split on line boundaries into ~size-char chunks that overlap by ~overlap chars,
    so an event straddling a boundary is seen whole by at least one chunk."""
    text = doc.text
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        piece = text[start:end]
        if piece.strip():
            chunks.append(Chunk(doc_index, len(chunks), start, piece))
        if end >= len(text):
            break
        next_start = max(end - overlap, start + 1)
        nl = text.find("\n", next_start, end)
        start = nl + 1 if nl != -1 else next_start
    return chunks


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_pdf(path: Path, rel: str) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(path)
    parts, starts, offset = [], [], 0
    for page in reader.pages:
        starts.append(offset)
        page_text = (page.extract_text() or "").strip() + "\n\n"
        parts.append(page_text)
        offset += len(page_text)
    return Document(rel, "".join(parts), page_starts=starts)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    paragraphs = []
    for para in root.iter(f"{_W}p"):
        bits = []
        for node in para.iter():
            if node.tag == f"{_W}t" and node.text:
                bits.append(node.text)
            elif node.tag == f"{_W}tab":
                bits.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                bits.append("\n")
        paragraphs.append("".join(bits))
    return "\n".join(paragraphs)


def _read_eml(path: Path) -> str:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    header = "\n".join(f"{h}: {msg[h]}" for h in ("Date", "From", "To", "Cc", "Subject") if msg[h])
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    return f"{header}\n\n{body}"


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
