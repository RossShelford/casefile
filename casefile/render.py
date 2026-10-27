"""Lay the Report out as a PDF: cover with legend, key events, who's who, at a glance,
the full log, things to check, sources.

Importance is shown four ways at once so it reads even in greyscale print or for
colour-blind readers: icon disc (red / amber / none), text colour (red / black / grey),
weight (bold for major and notable), and a tinted row for major events."""

from __future__ import annotations

import glob
import os
import re
import threading
import unicodedata
from collections import Counter
from contextvars import ContextVar
from itertools import groupby
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.fonts import ps2tt
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .icons import ASSETS, importance_icon
from .pipeline import Report
from .taxonomy import IMPORTANCE, IMPORTANCE_COLOR, IMPORTANCE_LABEL, IMPORTANCE_MEANING, KIND_ICON
from .timeline import Event, date_label, period_label

_STYLES = (("", "Regular"), ("-Bold", "Bold"), ("-Italic", "Italic"), ("-BoldItalic", "BoldItalic"))


def _register_family(name: str, files: dict[str, str]) -> dict[int, int]:
    """Register four styles as one family; returns the regular face's character map."""
    for style, _ in _STYLES:
        pdfmetrics.registerFont(TTFont(f"{name}{style}", files[style]))
    pdfmetrics.registerFontFamily(name, normal=name, bold=f"{name}-Bold", italic=f"{name}-Italic", boldItalic=f"{name}-BoldItalic")
    return pdfmetrics.getFont(name).face.charToGlyph


# Atkinson Hyperlegible: designed by the Braille Institute for low-vision readability (SIL OFL).
# It only covers Western European Latin, so Noto Sans (SIL OFL) fills in the rest of Latin,
# Greek, Cyrillic and Vietnamese. Both are bundled; together they are under 2 MB.
_FONTS = ASSETS / "fonts"
_FACES: list[tuple[str, dict[int, int]]] = [
    ("Body", _register_family("Body", {s: str(_FONTS / f"AtkinsonHyperlegible-{f}.ttf") for s, f in _STYLES})),
    ("Noto", _register_family("Noto", {s: str(_FONTS / f"NotoSans-{f}.ttf") for s, f in _STYLES})),
]


def _system_fallbacks() -> list[str]:
    """Fonts already on the computer for scripts we don't bundle (Chinese, Japanese, Korean,
    Thai, Arabic, Hebrew...). Bundling a CJK font would add 15+ MB, so we borrow one.
    reportlab reads only TrueType outlines, so CFF fonts (PingFang, Hiragino, Noto CJK) are
    skipped when they fail to load; the list goes on to one that works."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    paths = [
        # macOS
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/System/Library/Fonts/Supplemental/Tahoma.ttf",
        # Windows
        *(
            os.path.join(windir, "Fonts", f)
            for f in ("msyh.ttc", "arialuni.ttf", "YuGothR.ttc", "malgun.ttf", "tahoma.ttf", "Nirmala.ttf")
        ),
    ]
    # Linux: wherever the distribution put them.
    for pattern in (
        "NotoSansCJK*.ttc",
        "DroidSansFallbackFull.ttf",
        "wqy-microhei.ttc",
        "wqy-zenhei.ttc",
        "NotoSans*-Regular.ttf",
        "DejaVuSans.ttf",
    ):
        for root in ("/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.local/share/fonts")):
            paths += sorted(glob.glob(os.path.join(root, "**", pattern), recursive=True))
    return paths


_fallback_lock = threading.Lock()
_fallback_queue: list[str] | None = None  # candidates not yet tried; None until first needed
_face_cache: dict[str, str | None] = {}


def _load_fallback() -> bool:
    """Load the next usable system font; False once every candidate has been tried."""
    global _fallback_queue
    if _fallback_queue is None:
        _fallback_queue = _system_fallbacks()
    while _fallback_queue:
        path = _fallback_queue.pop(0)
        if not os.path.isfile(path):
            continue
        name = f"Fallback{len(_FACES) - 1}"
        try:
            font = TTFont(name, path)  # first face of a .ttc collection
        except (TTFError, OSError, ValueError, KeyError, IndexError):
            continue
        pdfmetrics.registerFont(font)
        # One style only: bold and italic text in this script shows as regular.
        pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)
        _FACES.append((name, font.face.charToGlyph))
        return True
    return False


def _face(ch: str) -> str | None:
    """The first font that has this character: Atkinson, then Noto Sans, then the system's."""
    if ch in _face_cache:
        return _face_cache[ch]
    with _fallback_lock:
        code = ord(ch)
        face = next((name for name, cmap in _FACES if code in cmap), None)
        while face is None and _load_fallback():
            name, cmap = _FACES[-1]
            face = name if code in cmap else None
        _face_cache[ch] = face
    return face


def covering_font(ch: str) -> str | None:
    """Registered font name that will draw `ch`, or None if no available font has it."""
    return _face(ch)


# Characters no font could draw while building the current PDF (one set per render call).
_missing: ContextVar[set[str] | None] = ContextVar("casefile_missing_glyphs", default=None)
MISSING_GLYPHS_NOTE = "Some characters couldn't be shown: install a font that covers them."
FAILED_PARTS_NOTE = (
    "These parts were skipped and will stay skipped on a rerun. To try them again, delete this "
    "report and choose the folder again (command line: casefile clear-cache)."
)


def _status(doc) -> str:
    """'ok' plus how many repeated lines (headers, footers, quoted copies) were read once."""
    if doc.status == "ok" and doc.repeats_removed:
        return f"ok, {doc.repeats_removed:,} repeated lines read once"
    return doc.status


def _printable(text: str) -> str:
    """Control characters become spaces and invisible formatting characters (zero-width
    joiners, direction marks) are dropped: they have no glyph and would print as boxes."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc":
            out.append(" ")
        elif cat not in ("Cf", "Cs"):
            out.append(ch)
    return "".join(out)


def _word_faces(word: str, missing: set[str] | None) -> list[str]:
    """Font per character of one word. A word that needs a fallback for some letters is set
    wholly in that fallback when it has them all ("Nguyễn" in Noto, not "Nguy" in Atkinson
    and "ễn" in Noto), so a word never mixes two designs."""
    faces = [_face(ch) for ch in word]
    for ch, face in zip(word, faces):
        if face is None and missing is not None:
            missing.add(ch)
    found = {f for f in faces if f is not None}
    if len(found) > 1:
        order = [name for name, _ in _FACES]
        widest = max(found, key=order.index)
        cmap = _FACES[order.index(widest)][1]
        if all(ord(ch) in cmap for ch, f in zip(word, faces) if f is not None):
            faces = [widest if f is not None else None for f in faces]
    return [f or "Body" for f in faces]  # no font: kept, so the reader sees a box there


def _runs(text: str) -> list[tuple[str, str]]:
    """Split text into (font, run) pieces. Spaces stay with the run they're in, so a
    Cyrillic sentence is one run rather than one per word."""
    runs: list[tuple[str, str]] = []
    missing = _missing.get()
    for piece in re.split(r"(\s+)", _printable(text)):
        if not piece:
            continue
        faces = [runs[-1][0] if runs else "Body"] * len(piece) if piece.isspace() else _word_faces(piece, missing)
        for ch, face in zip(piece, faces):
            if runs and runs[-1][0] == face:
                runs[-1] = (face, runs[-1][1] + ch)
            else:
                runs.append((face, ch))
    return runs


def _markup(text: str, font_name: str) -> str:
    """Escaped Paragraph markup for plain text, switching font for characters the body font
    lacks. `font_name` is the paragraph's own font: a <font face> tag resets bold and
    italic, so each switch names the matching style of the fallback family."""
    _, bold, italic = ps2tt(font_name)
    suffix = {(0, 0): "", (1, 0): "-Bold", (0, 1): "-Italic", (1, 1): "-BoldItalic"}[(int(bold), int(italic))]
    out = []
    for face, run in _runs(text):
        if face == "Body":
            out.append(escape(run))
        else:
            out.append(f'<font face="{face}{suffix if face == "Noto" else ""}">{escape(run)}</font>')
    return "".join(out)


def _draw(canvas, x: float, y: float, text: str, size: float, right: bool = False) -> None:
    """canvas.drawString with the same font fallback as paragraphs (for the page footer)."""
    runs = _runs(text)
    if right:
        x -= sum(pdfmetrics.stringWidth(run, face, size) for face, run in runs)
    for face, run in runs:
        canvas.setFont(face, size)
        canvas.drawString(x, y, run)
        x += pdfmetrics.stringWidth(run, face, size)


# Longest text shown per table cell. A model can return a whole page as one "quote"; past
# a few hundred characters it stops helping the reader, and a table row taller than a page
# makes reportlab give up on the entire PDF.
LIMIT = {
    "summary": 600,
    "quote": 600,
    "source": 240,
    "name": 80,
    "full_name": 120,
    "role": 100,
    "path": 300,
    "error": 600,
    "title": 200,
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"



BLACK = HexColor(IMPORTANCE_COLOR["standard"])
GREY = HexColor(IMPORTANCE_COLOR["minor"])
RED = HexColor(IMPORTANCE_COLOR["major"])
META = HexColor("#5F646C")  # sources, labels: quieter than black, more legible than minor grey
RULE = HexColor("#DADDE1")
RED_TINT = HexColor("#FDECEE")

S = {
    "title": ParagraphStyle("title", fontName="Body-Bold", fontSize=26, leading=32, textColor=BLACK, spaceAfter=4),
    "subtitle": ParagraphStyle("subtitle", fontName="Body", fontSize=10.5, leading=15, textColor=META, spaceAfter=16),
    "h1": ParagraphStyle("h1", fontName="Body-Bold", fontSize=17, leading=22, textColor=BLACK, spaceAfter=8),
    "h2": ParagraphStyle("h2", fontName="Body-Bold", fontSize=12, leading=16, textColor=BLACK, spaceBefore=12, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Body", fontSize=10, leading=14.5, textColor=BLACK),
    "cell": ParagraphStyle("cell", fontName="Body", fontSize=9, leading=12.5, textColor=BLACK),
    "cellb": ParagraphStyle("cellb", fontName="Body-Bold", fontSize=9, leading=12.5, textColor=BLACK),
    "small": ParagraphStyle("small", fontName="Body", fontSize=7.5, leading=10, textColor=META),
    "quote": ParagraphStyle("quote", fontName="Body-Italic", fontSize=8, leading=11, textColor=META, spaceBefore=2),
    "head": ParagraphStyle("head", fontName="Body-Bold", fontSize=8, leading=10, textColor=META),
    "warn": ParagraphStyle("warn", fontName="Body", fontSize=10, leading=14.5, textColor=META, spaceAfter=6),
}
# The event sentence itself carries the importance: red bold, black bold, black, grey.
SUMMARY = {
    "major": ParagraphStyle("major", parent=S["cell"], fontName="Body-Bold", textColor=RED),
    "notable": ParagraphStyle("notable", parent=S["cell"], fontName="Body-Bold"),
    "standard": S["cell"],
    "minor": ParagraphStyle("minor", parent=S["cell"], textColor=GREY),
}
ICON = 12  # points, in log rows
WIDTH = A4[0] - 30 * mm


def _p(text: str, style: str | ParagraphStyle = "cell") -> Paragraph:
    style = S[style] if isinstance(style, str) else style
    return Paragraph(_markup(text, style.fontName), style)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _table(rows: list[list], widths: list[float], header: list[str] | None, extra: list | None = None) -> Table:
    data = ([[_p(h, "head") for h in header]] if header else []) + rows
    # Cell text is clipped (LIMIT) so rows stay short. splitInRow is the backstop for a row
    # still taller than the space left: reportlab first tries whole rows, and only then splits
    # inside one (leaving at least 36 points, about three lines) instead of failing the PDF.
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0, splitInRow=36)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style.append(("LINEBELOW", (0, 0), (-1, 0), 1, BLACK))
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def _event_table(events: list[Event], quotes: bool, date_header: str = "Date") -> Table:
    rows, extra = [], []
    for i, ev in enumerate(events, 1):  # row 0 is the header
        cell = [_p(_clip(ev.summary, LIMIT["summary"]), SUMMARY[ev.importance])]
        if quotes and ev.quote:
            cell.append(_p(f"“{_clip(ev.quote, LIMIT['quote'])}”", "quote"))
        when = date_label(ev.date) if ev.date else _clip(ev.date_as_written or "—", LIMIT["name"])
        rows.append(
            [importance_icon(KIND_ICON[ev.kind], ev.importance, ICON), _p(when), cell, _p(_clip(ev.source, LIMIT["source"]), "small")]
        )
        if ev.importance == "major":
            extra.append(("BACKGROUND", (0, i), (-1, i), RED_TINT))
    widths = [ICON + 8, 22 * mm, WIDTH - ICON - 8 - 22 * mm - 44 * mm, 44 * mm]
    return _table(rows, widths, ["", date_header, "What happened", "Source"], extra)


class _Anchor(Flowable):
    """Invisible marker: a target for in-document links, optionally also listed in the
    PDF viewer's sidebar outline so a 1,000-page report can be navigated by month."""

    def __init__(self, key: str, title: str | None = None, level: int = 0):
        super().__init__()
        self.key, self.title, self.level = key, title, level

    def wrap(self, avail_width, avail_height):
        return 0, 0

    def draw(self):
        self.canv.bookmarkPage(self.key)
        if self.title:
            self.canv.addOutlineEntry(self.title, self.key, level=self.level)


def _section(key: str, title: str) -> list:
    return [PageBreak(), _Anchor(key, title, 0), _p(title, "h1")]


def _link(key: str, text: str) -> Paragraph:
    return Paragraph(f'<a href="#{key}"><u>{_markup(text, S["cellb"].fontName)}</u></a>', S["cellb"])


def render(report: Report, out_path: str, *, quotes: bool = True) -> None:
    token = _missing.set(set())
    try:
        _render(report, out_path, quotes)
    finally:
        _missing.reset(token)


def _render(report: Report, out_path: str, quotes: bool) -> None:
    story: list = []
    dated = [e for e in report.events if e.date]
    counts = Counter(e.importance for e in report.events)
    major = [e for e in report.events if e.importance == "major"]
    title = _clip(report.title, LIMIT["title"])

    # -- Cover ------------------------------------------------------------------
    span = f"{date_label(dated[0].date)} to {date_label(dated[-1].date)}" if dated else "No dated events"
    read = sum(1 for d in report.documents if d.status == "ok")
    story += [
        _p(title, "title"),
        _p(
            f"{span}. Built {report.generated:%d %b %Y} from {_plural(read, 'file')} in “{_clip(report.folder, LIMIT['path'])}” "
            f"by {_clip(report.model, LIMIT['name'])}, running on this computer.",
            "subtitle",
        ),
    ]
    missing_note_at = len(story)  # filled in once every piece of text has been laid out
    story.append(Paragraph("Importance", S["h2"]))
    legend = []
    for level in reversed(IMPORTANCE):
        legend.append(
            [
                importance_icon(KIND_ICON["other"], level, 14),
                _p(IMPORTANCE_LABEL[level], SUMMARY[level]),
                _p(IMPORTANCE_MEANING[level]),
                _p(_plural(counts.get(level, 0), "event"), "cellb"),
            ]
        )
    story.append(_table(legend, [22, 22 * mm, WIDTH - 22 - 22 * mm - 26 * mm, 26 * mm], None))

    story.append(Paragraph("Icons", S["h2"]))
    kinds = list(KIND_ICON.items())
    grid = []
    for row in (kinds[:5], kinds[5:]):
        cells = []
        for kind, name in row:
            cells += [importance_icon(name, "standard", ICON), _p(kind.capitalize())]
        grid.append(cells)
    story.append(_table(grid, [ICON + 8, WIDTH / 5 - ICON - 8] * 5, None, [("LINEBELOW", (0, 0), (-1, -1), 0, white)]))

    story.append(Paragraph("How to read this", S["h2"]))
    for line in (
        "<b>Key events</b> lists the major events first. <b>At a glance</b> shows each month and who was involved; "
        "click a month to jump to it. The PDF sidebar lists every year and month.",
        "People are called by first name. When two share a first name, their surname is added. "
        "Full names are in <b>Who’s who</b>.",
        "Every event names the file and page (or line) it came from, with the exact words that support it. "
        f"{_plural(len(report.unverified), 'event')} whose words could not be found in the file "
        "are kept apart under <b>Needs checking</b>.",
        "An AI model wrote this summary and judged importance. It can miss or misread things: "
        "check the source before relying on any entry.",
    ):
        story += [Paragraph(line, S["body"]), Spacer(1, 2 * mm)]

    # -- Key events ---------------------------------------------------------------
    if major:
        story += _section("key", "Key events")
        story.append(
            _p(
                f"The {_plural(len(major), 'major event')}, in date order. "
                f"{_plural(counts.get('notable', 0), 'notable event')} are shown in bold with an amber mark in the full log.",
                "warn",
            )
        )
        story.append(_event_table(major, quotes))

    # -- Who's who --------------------------------------------------------------
    story += _section("people", "Who’s who")
    if report.people:
        rows = []
        for p in report.people:
            active = ""
            if p.first_event:
                first, last = date_label(p.first_event), date_label(p.last_event)
                active = first if first == last else f"{first} to {last}"
            rows.append(
                [
                    _p(_clip(p.label, LIMIT["name"]), "cellb"),
                    _p(_clip(p.full_name, LIMIT["full_name"])),
                    _p("; ".join(_clip(role, LIMIT["role"]) for role in p.top_roles())),
                    _p(str(p.events)),
                    _p(active, "small"),
                    _p(_clip(p.first_seen[2], LIMIT["source"]), "small"),
                ]
            )
        story.append(
            _table(
                rows,
                [30 * mm, 34 * mm, WIDTH - 138 * mm, 16 * mm, 26 * mm, 32 * mm],
                ["Name", "Full name", "Role", "Events", "Active", "First appears"],
            )
        )
    else:
        story.append(_p("No people were identified.", "body"))

    months = [(period, list(group)) for period, group in groupby(dated, key=lambda e: e.date[:7])]

    # -- At a glance ------------------------------------------------------------
    if months:
        story += _section("glance", "At a glance")
        story.append(_p("Each month: how much happened, how much of it was major, and who was most involved.", "warn"))
        rows = []
        for period, group in months:
            involved = Counter(_clip(p.label, LIMIT["name"]) for e in group for p in e.actors)
            n_major = sum(1 for e in group if e.importance == "major")
            rows.append(
                [
                    _link(f"m{period}", period_label(period)),
                    _p(f"{len(group):,}"),
                    _p(str(n_major) if n_major else "", SUMMARY["major"]),
                    _p(", ".join(f"{name} ({n})" for name, n in involved.most_common(4))),
                ]
            )
        story.append(_table(rows, [34 * mm, 16 * mm, 16 * mm, WIDTH - 66 * mm], ["Month", "Events", "Major", "Most involved"]))

    # -- What happened ----------------------------------------------------------
    story += _section("log", "What happened")
    if not report.events:
        story.append(_p("No events were identified.", "body"))
    year = None
    for period, group in months:
        # Start the month on a new page rather than leave its heading stranded at the bottom.
        story.append(CondPageBreak(35 * mm))
        if period[:4] != year:
            year = period[:4]
            story.append(_Anchor(f"y{year}", year, 1))
        story += [
            _Anchor(f"m{period}", period_label(period), 2),
            _p(period_label(period), "h2"),
            _event_table(group, quotes),
        ]
    undated = [e for e in report.events if not e.date]
    if undated:
        story += [CondPageBreak(35 * mm), _Anchor("undated", "Undated", 1), Paragraph("Undated", S["h2"])]
        story.append(_event_table(undated, quotes, "Date given"))

    # -- Needs checking ---------------------------------------------------------
    if report.unverified:
        story += _section("check", "Needs checking")
        story.append(
            _p(
                "The AI reported these, but the words it quoted were not found in the file. "
                "Treat them as leads, not facts.",
                "warn",
            )
        )
        story.append(_event_table(report.unverified, True))

    # -- Sources ----------------------------------------------------------------
    story += _section("sources", "Sources")
    rows = [
        [
            _p(_clip(d.path, LIMIT["path"])),
            _p(str(report.events_per_doc.get(d.path, 0)) if d.status == "ok" else "—"),
            _p(_clip(_status(d), LIMIT["path"]), "small"),
        ]
        for d in sorted(report.documents, key=lambda d: d.path)
    ]
    story.append(_table(rows, [WIDTH - 70 * mm, 16 * mm, 54 * mm], ["File", "Events", "Status"]))
    if report.failures:
        story.append(Paragraph("Parts the AI could not process", S["h2"]))
        story.append(_p(FAILED_PARTS_NOTE, "small"))
        rows = [[_p(_clip(src, LIMIT["source"]), "small"), _p(_clip(err, LIMIT["error"]), "small")] for src, err in report.failures]
        story.append(_table(rows, [60 * mm, WIDTH - 60 * mm], ["Where", "Error"]))

    if _missing.get():
        story.insert(missing_note_at, _p(MISSING_GLYPHS_NOTE, "warn"))

    footer_title = _clip(report.title, 90)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(META)
        _draw(canvas, 15 * mm, 10 * mm, f"{footer_title}. AI-generated: check the sources.", 7.5)
        _draw(canvas, A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}", 7.5, right=True)
        canvas.restoreState()

    def first_page(canvas, doc):
        canvas.showOutline()  # open the sidebar outline when the PDF is opened
        footer(canvas, doc)

    SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=report.title,
        author="casefile",
    ).build(story, onFirstPage=first_page, onLaterPages=footer)
