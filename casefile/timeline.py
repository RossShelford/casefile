"""Turn raw extracted events into a checked, ordered, de-duplicated activity log."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from functools import lru_cache

from .people import Person
from .taxonomy import RANK

_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "})


@dataclass
class Event:
    date: str  # "YYYY-MM-DD", "YYYY-MM", "YYYY", or "" when unknown
    date_as_written: str  # what the model gave, shown when it could not be normalised
    summary: str
    quote: str
    verified: bool  # quote found in the source text
    source: str  # "folder/file.pdf, p. 3"
    doc_index: int
    offset: int
    actors: list[Person] = field(default_factory=list)
    kind: str = "other"  # taxonomy.KINDS
    importance: str = "standard"  # taxonomy.IMPORTANCE

    @property
    def sort_key(self) -> tuple:
        return (self.date, self.doc_index, self.offset)


def date_label(iso: str) -> str:
    """'2023-04-05' -> '5 Apr 2023', '2023-04' -> 'Apr 2023', '2023' -> '2023'."""
    try:
        if len(iso) == 10:
            d = date.fromisoformat(iso)
            return f"{d.day} {d:%b %Y}"
        if len(iso) == 7:
            return date.fromisoformat(iso + "-01").strftime("%b %Y")
    except ValueError:
        pass
    return iso


def period_label(iso: str) -> str:
    """Heading for a month group: '2023-04...' -> 'April 2023'; a bare year stays as is."""
    return date.fromisoformat(iso[:7] + "-01").strftime("%B %Y") if len(iso) >= 7 else iso


def normalize_date(raw: str) -> str:
    """Accept unambiguous dates only. '03/04/2023' could be March or April, so it is
    left undated (and shown as written) rather than guessed."""
    raw = raw.strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return ""
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw)
    if m:
        return raw if 1 <= int(m[2]) <= 12 else ""
    if re.fullmatch(r"\d{4}", raw):
        return raw
    for fmt, out in (
        ("%B %d, %Y", "%Y-%m-%d"), ("%b %d, %Y", "%Y-%m-%d"), ("%d %B %Y", "%Y-%m-%d"),
        ("%d %b %Y", "%Y-%m-%d"), ("%B %Y", "%Y-%m"), ("%b %Y", "%Y-%m"),
    ):
        try:
            return datetime.strptime(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw), fmt).strftime(out)
        except ValueError:
            continue
    return ""


def _normalized(text: str) -> tuple[str, list[int]]:
    """Lower-case, unify quotes/dashes, collapse whitespace; keep a map back to original offsets."""
    out: list[str] = []
    index: list[int] = []
    prev_space = True
    for i, ch in enumerate(text.translate(_TRANSLATE)):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                index.append(i)
            prev_space = True
            continue
        prev_space = False
        for c in ch.casefold():
            out.append(c)
            index.append(i)
    return "".join(out), index


def locate_quote(quote: str, text: str) -> int | None:
    """Offset of the quote inside text, or None if the text does not contain it.

    Tolerates case, whitespace, curly quotes and '...' elisions; otherwise requires a
    near-exact match (>=90% of the quote as one unbroken run), because the quote is
    the reader's proof that the event is real and not invented by the model."""
    norm_text, index = _normalized_source(text)
    parts = [p for p in (_normalized(p)[0].strip(" \"'") for p in re.split(r"\.\.\.|…", quote)) if len(p) >= 4]
    if not parts:
        return None
    pos, first = 0, None
    for part in parts:
        found = norm_text.find(part, pos)
        if found == -1:
            break
        first = found if first is None else first
        pos = found + len(part)
    else:
        return index[first]
    whole = " ".join(parts)
    if len(whole) < 20:
        return None
    match = SequenceMatcher(None, norm_text, whole, autojunk=False).find_longest_match(0, len(norm_text), 0, len(whole))
    return index[match.a] if match.size >= 0.9 * len(whole) else None


# Every event in a part is checked against the same text; normalise it once, not per event.
_normalized_source = lru_cache(maxsize=16)(_normalized)


def deduplicate(events: list[Event]) -> list[Event]:
    """Chunks overlap, and documents repeat each other (reply chains, copies). An event is a
    duplicate only when the evidence is the same: its quote equals an earlier one (anywhere),
    or contains / is contained in one from the same date. Then it must also involve the same
    people or say much the same thing, because one sentence can support two different events.

    Summaries alone never make a duplicate: "Sarah paid Tom" and "Sarah phoned Tom" on the
    same day read alike but are different events. A repeated entry is better than a lost one.
    Verified and dated copies are kept in preference to the rest. The kept copy takes the
    highest importance any copy was given, so a major event is never shown as minor."""
    kept: list[Event] = []
    by_quote: dict[str, list[tuple[str, frozenset, Event]]] = {}
    by_date: dict[str, list[tuple[str, str, frozenset, Event]]] = {}
    for ev in sorted(events, key=lambda e: (not e.verified, not e.date, e.sort_key)):
        quote = _normalized(ev.quote)[0]
        summary = ev.summary.casefold()
        actors = frozenset(p.id for p in ev.actors)
        if quote:
            same_day = by_date.setdefault(ev.date or f"undated:{ev.doc_index}", [])
            original = next(
                (k for s, a, k in by_quote.get(quote, ()) if a == actors or _similar(s, summary)),
                None,
            ) or next(
                (k for q, s, a, k in same_day if _overlaps(q, quote) and (a == actors or _similar(s, summary))),
                None,
            )
            if original is not None:
                if RANK[ev.importance] > RANK[original.importance]:
                    original.importance = ev.importance
                continue
            by_quote.setdefault(quote, []).append((summary, actors, ev))
            same_day.append((quote, summary, actors, ev))
        kept.append(ev)
    return sorted(kept, key=lambda e: e.sort_key)


def _overlaps(a: str, b: str) -> bool:
    short, long = sorted((a, b), key=len)
    return len(short) >= 20 and short in long


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= 0.6
