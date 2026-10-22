"""Lay the Report out as a PDF: cover with legend, key events, who's who, at a glance,
the full log, things to check, sources.

Importance is shown four ways at once so it reads even in greyscale print or for
colour-blind readers: icon disc (red / amber / none), text colour (red / black / grey),
weight (bold for major and notable), and a tinted row for major events."""

from __future__ import annotations

from collections import Counter
from itertools import groupby
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
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

# Atkinson Hyperlegible: designed by the Braille Institute for low-vision readability (SIL OFL).
for _style, _file in (("", "Regular"), ("-Bold", "Bold"), ("-Italic", "Italic"), ("-BoldItalic", "BoldItalic")):
    pdfmetrics.registerFont(TTFont(f"Body{_style}", str(ASSETS / "fonts" / f"AtkinsonHyperlegible-{_file}.ttf")))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-BoldItalic")

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
    return Paragraph(escape(text), S[style] if isinstance(style, str) else style)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _table(rows: list[list], widths: list[float], header: list[str] | None, extra: list | None = None) -> Table:
    data = ([[_p(h, "head") for h in header]] if header else []) + rows
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
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
        cell = [_p(ev.summary, SUMMARY[ev.importance])]
        if quotes and ev.quote:
            cell.append(_p(f"“{ev.quote}”", "quote"))
        when = date_label(ev.date) if ev.date else (ev.date_as_written or "—")
        rows.append([importance_icon(KIND_ICON[ev.kind], ev.importance, ICON), _p(when), cell, _p(ev.source, "small")])
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
    return [PageBreak(), _Anchor(key, title, 0), Paragraph(escape(title), S["h1"])]


def _link(key: str, text: str) -> Paragraph:
    return Paragraph(f'<a href="#{key}"><u>{escape(text)}</u></a>', S["cellb"])


def render(report: Report, out_path: str, *, quotes: bool = True) -> None:
    story: list = []
    dated = [e for e in report.events if e.date]
    counts = Counter(e.importance for e in report.events)
    major = [e for e in report.events if e.importance == "major"]

    # -- Cover ------------------------------------------------------------------
    span = f"{date_label(dated[0].date)} to {date_label(dated[-1].date)}" if dated else "No dated events"
    read = sum(1 for d in report.documents if d.status == "ok")
    story += [
        Paragraph(escape(report.title), S["title"]),
        Paragraph(
            escape(
                f"{span}. Built {report.generated:%d %b %Y} from {_plural(read, 'file')} in “{report.folder}” "
                f"by {report.model}, running on this computer."
            ),
            S["subtitle"],
        ),
        Paragraph("Importance", S["h2"]),
    ]
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
                    _p(p.label, "cellb"),
                    _p(p.full_name),
                    _p("; ".join(p.top_roles())),
                    _p(str(p.events)),
                    _p(active, "small"),
                    _p(p.first_seen[2], "small"),
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
            involved = Counter(p.label for e in group for p in e.actors)
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
            Paragraph(escape(period_label(period)), S["h2"]),
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
        [_p(d.path), _p(str(report.events_per_doc.get(d.path, 0)) if d.status == "ok" else "—"), _p(d.status, "small")]
        for d in sorted(report.documents, key=lambda d: d.path)
    ]
    story.append(_table(rows, [WIDTH - 70 * mm, 16 * mm, 54 * mm], ["File", "Events", "Status"]))
    if report.failures:
        story.append(Paragraph("Parts the AI could not process", S["h2"]))
        rows = [[_p(src, "small"), _p(err, "small")] for src, err in report.failures]
        story.append(_table(rows, [60 * mm, WIDTH - 60 * mm], ["Where", "Error"]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 7.5)
        canvas.setFillColor(META)
        canvas.drawString(15 * mm, 10 * mm, f"{report.title}. AI-generated: check the sources.")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
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
