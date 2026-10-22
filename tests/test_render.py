"""PDF rendering: long text never breaks the layout, and non-Latin names print."""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import datetime

import pytest
from pypdf import PdfReader

from casefile import render as R
from casefile.ingest import Document
from casefile.people import Person
from casefile.pipeline import Report
from casefile.timeline import Event

CYRILLIC = "Жанна Иванова"
GREEK = "Νίκος Παπαδόπουλος"
CJK = "王小明"


def _event(summary: str, quote: str, date: str = "2023-04-05", importance: str = "major", **kw) -> Event:
    return Event(
        date=date,
        date_as_written=kw.pop("date_as_written", date),
        summary=summary,
        quote=quote,
        verified=kw.pop("verified", True),
        source=kw.pop("source", "letters/notice.pdf, p. 1"),
        doc_index=0,
        offset=0,
        importance=importance,
        **kw,
    )


def _report(events: list[Event], unverified: list[Event] = (), people: list[Person] = (), **kw) -> Report:
    fields = {f.name for f in dataclasses.fields(Report)}
    extra = {"cache_keys": set()} if "cache_keys" in fields else {}
    return Report(
        title=kw.pop("title", "Test case"),
        folder="case",
        model="test-model",
        generated=datetime(2026, 10, 22),
        documents=kw.pop("documents", [Document(path="letters/notice.pdf", text="x")]),
        people=list(people),
        events=events,
        unverified=list(unverified),
        **kw,
        **extra,
    )


def _person(pid: int, name: str, role: str = "tenant") -> Person:
    first, _, last = name.partition(" ")
    return Person(
        id=pid,
        label=first,
        full_name=name,
        first=first,
        last=last,
        roles=Counter({role: 1}),
        first_seen=(0, 0, "letters/notice.pdf, p. 1"),
        events=1,
        first_event="2023-04-05",
        last_event="2023-04-05",
    )


def _text(path) -> tuple[str, str]:
    """(first page text, whole document text)."""
    pages = [p.extract_text() for p in PdfReader(str(path)).pages]
    return pages[0], "\n".join(pages)


def _fonts(path) -> set[str]:
    names = set()
    for page in PdfReader(str(path)).pages:
        for font in (page.get("/Resources", {}).get("/Font") or {}).values():
            names.add(str(font.get_object()["/BaseFont"]))
    return names


def test_long_and_multilingual_report_renders(tmp_path):
    long_quote = " ".join(f"word{i}" for i in range(1500))
    long_summary = "The landlord wrote again about the repairs and the deposit. " * 10  # ~600 chars
    assert len(long_summary) >= 600
    people = [_person(1, CYRILLIC), _person(2, GREEK, "Λογιστής"), _person(3, CJK, "房东")]
    actors = {"actors": people}
    events = [
        _event(long_summary, long_quote, **actors),
        _event(f"{CYRILLIC} met {GREEK} and {CJK}.", "short words here in the file", "2023-05-01", "notable"),
        _event(f"Встреча с {CYRILLIC}", "Подпись: Жанна Иванова", "", "standard", date_as_written="весной"),
    ]
    unverified = [_event(long_summary, long_quote, verified=False)]
    report = _report(
        events,
        unverified,
        people,
        title=f"Дело {CYRILLIC}",
        failures=[("letters/notice.pdf (1/1)", "ExtractionError: " + "bad output " * 200)],
    )
    out = tmp_path / "report.pdf"
    R.render(report, str(out))

    first, text = _text(out)
    assert CYRILLIC in text
    assert "Дело" in first
    # The 1,500-word quote is cut, not printed in full.
    assert "word1499" not in text
    assert "…" in text

    used = {ch for ch in long_summary + CYRILLIC + GREEK + CJK + "Λογιστής房东Встреча сПодписьвесной" if ord(ch) > 127}
    uncovered = {ch for ch in used if R.covering_font(ch) is None}
    assert not uncovered or R.MISSING_GLYPHS_NOTE in first
    if not uncovered:
        assert R.MISSING_GLYPHS_NOTE not in first


def test_bold_cyrillic_uses_bold_fallback_face(tmp_path):
    # A major summary is bold; the Cyrillic run inside must stay bold, not drop to regular.
    out = tmp_path / "report.pdf"
    R.render(_report([_event(f"{CYRILLIC} signed the lease.", "signed the lease on the fifth of April")]), str(out))
    assert any("NotoSans-Bold" in name for name in _fonts(out))


def test_unbroken_cjk_text_and_huge_rows_still_render(tmp_path):
    # No spaces to break on, every cell at its limit: the PDF must still build.
    wall = CJK * 2000
    ev = _event(wall, wall, source=wall)
    report = _report([ev], [ev], [_person(1, f"{wall} {wall}", wall)], documents=[Document(path=wall, status=wall)])
    out = tmp_path / "report.pdf"
    R.render(report, str(out))
    assert out.stat().st_size > 0


def test_table_row_taller_than_a_page_splits_instead_of_failing(tmp_path):
    from reportlab.platypus import SimpleDocTemplate

    cell = [R._p("word " * 3000), R._p("quote " * 2000, "quote")]
    table = R._table([[R._p("5 Apr 2023"), cell]], [60, 400], ["Date", "What happened"])
    SimpleDocTemplate(str(tmp_path / "t.pdf")).build([table])


def test_missing_glyph_note_on_cover(tmp_path, monkeypatch):
    # Pretend no font on this computer has CJK: the text is kept and the cover says so.
    real = R._face
    monkeypatch.setattr(R, "_face", lambda ch: None if ch in CJK else real(ch))
    out = tmp_path / "report.pdf"
    R.render(_report([_event(f"{CJK} paid the rent.", "paid the rent in full on time")], people=[_person(1, CJK)]), str(out))
    first, _ = _text(out)
    assert R.MISSING_GLYPHS_NOTE in first


def test_no_note_when_everything_is_covered(tmp_path):
    out = tmp_path / "report.pdf"
    R.render(_report([_event(f"{CYRILLIC} paid the rent.", "paid the rent in full on time")]), str(out))
    first, _ = _text(out)
    assert R.MISSING_GLYPHS_NOTE not in first


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 10, "short"),
        ("a  b\n\nc", 10, "a b c"),
        ("abcdefghij", 5, "abcd…"),
    ],
)
def test_clip(text, limit, expected):
    assert R._clip(text, limit) == expected
    assert len(R._clip(text, limit)) <= limit


def test_markup_escapes_and_switches_font():
    m = R._markup(f"<b> & {CYRILLIC}", "Body-Bold")
    assert m.startswith("&lt;b&gt; &amp; ")
    assert m.endswith(f'<font face="Noto-Bold">{CYRILLIC}</font>')
    # Control and zero-width characters never reach the PDF.
    assert R._markup("a\x00b\u200bc", "Body") == "a bc"


def test_word_needing_fallback_is_set_in_one_font():
    # "Nguyễn": Atkinson lacks "ễ"; the whole word goes to Noto Sans rather than mixing designs.
    assert R._runs("Mr Nguyễn Văn") == [("Body", "Mr "), ("Noto", "Nguyễn "), ("Body", "Văn")]
