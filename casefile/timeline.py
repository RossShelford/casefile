"""Turn raw extracted events into a checked, ordered, de-duplicated activity log."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
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


# Month names (accents removed, lower case) in English, French, German, Spanish, Italian,
# Dutch and Portuguese, with their usual abbreviations. A written month is never ambiguous;
# only all-number dates such as 03/04/2023 are.
_MONTH_NAMES = (
    "january jan janvier janv januar janner jan enero ene gennaio gen januari janeiro",
    "february feb fevrier fevr fev februar febrero febbraio februari fevereiro",
    "march mar mars marz maerz mrz marzo maart mrt marco",
    "april apr avril avr abril abr aprile",
    "may mai mayo maggio mag mei maio",
    "june jun juin juni junio giugno giu junho",
    "july jul juillet juil juli julio luglio lug julho",
    "august aug aout agosto ago augustus",
    "september sep sept septembre septiembre setiembre settembre set setembro",
    "october oct octobre oktober okt octubre ottobre ott outubro out",
    "november nov novembre noviembre novembro",
    "december dec decembre dezember dez diciembre dic dicembre dezembro",
)
MONTHS = {name: number for number, names in enumerate(_MONTH_NAMES, 1) for name in names.split()}
WEEKDAYS = set(
    "monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun "
    "lundi mardi mercredi jeudi vendredi samedi dimanche "
    "montag dienstag mittwoch donnerstag freitag samstag sonnabend sonntag "
    "lunes martes miercoles jueves viernes sabado domingo "
    "lunedi martedi mercoledi giovedi venerdi sabato domenica "
    "maandag dinsdag woensdag donderdag vrijdag zaterdag zondag "
    "segunda-feira terca-feira quarta-feira quinta-feira sexta-feira segunda terca quarta quinta sexta".split()
)
# Words between the parts of a date: "5 de marzo de 2023", "the 5th of April", "le 5 mars".
_FILLERS = {"de", "del", "of", "the", "le"}


def normalize_date(raw: str) -> str:
    """Accept unambiguous dates only. '03/04/2023' could be March or April, so it is
    left undated (and shown as written) rather than guessed.

    Written months are fine in any order and in several languages: 'April 5 2023',
    'Monday, 5 April 2023', 'Sept. 2023', '5 mars 2023', '5. März 2023', '5 de marzo de 2023'."""
    raw = raw.strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw)
    if m:
        return raw if 1 <= int(m[2]) <= 12 else ""
    if re.fullmatch(r"\d{4}", raw):
        return raw
    text = "".join(c for c in unicodedata.normalize("NFKD", raw.casefold()) if not unicodedata.combining(c))
    text = re.sub(r"(\d)(st|nd|rd|th|er|re|eme|e|o|a)\b", r"\1", text)  # 5th, 1er, 1º
    words = [w for w in re.split(r"[\s,.]+", text) if w and w not in _FILLERS]
    if words and words[0] in WEEKDAYS:
        words = words[1:]
    shape = "".join("D" if re.fullmatch(r"\d{1,2}", w) else "Y" if re.fullmatch(r"\d{4}", w)
                    else "M" if w in MONTHS else "?" for w in words)
    if shape == "DMY":
        return _iso(int(words[2]), MONTHS[words[1]], int(words[0]))
    if shape == "MDY":
        return _iso(int(words[2]), MONTHS[words[0]], int(words[1]))
    if shape == "MY":
        return f"{int(words[1]):04d}-{MONTHS[words[0]]:02d}"
    return ""


def _iso(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
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


MIN_QUOTE_WORDS = 5
MIN_QUOTE_CHARS = 25
MIN_PIECE_WORDS = 3
# A "word" for the minimums above ("won't" and "5,000" are one word). Chinese, Japanese
# and Thai don't put spaces between words, so each of their characters counts as one.
_WORD = re.compile(r"[\u0e00-\u0e7f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[^\W_]+(?:[',.][^\W_]+)*")


def _words(text: str) -> int:
    return len(_WORD.findall(text))


def locate_quote(quote: str, text: str) -> int | None:
    """Offset of the quote inside text, or None if the text does not contain it.

    Tolerates case, whitespace, curly quotes and '...' elisions; otherwise requires a
    near-exact match (>=90% of the quote as one unbroken run), because the quote is
    the reader's proof that the event is real and not invented by the model.

    A quote only counts if what matched is real evidence: at least 5 words and 25
    characters in all, and every '...' piece used at least 3 words. Otherwise a quote
    such as "paid" or "Sarah ... paid ... £5,000" would 'verify' almost any invention."""
    norm_text, index = _normalized_source(text)
    pieces = (_normalized(p)[0].strip(" \"'") for p in re.split(r"\.\.\.|…", quote))
    parts = [p for p in pieces if _words(p) >= MIN_PIECE_WORDS]
    if not _enough(parts):
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
    match = SequenceMatcher(None, norm_text, whole, autojunk=False).find_longest_match(0, len(norm_text), 0, len(whole))
    matched = whole[match.b : match.b + match.size]
    return index[match.a] if match.size >= 0.9 * len(whole) and _enough([matched]) else None


def _enough(parts: list[str]) -> bool:
    return sum(map(_words, parts)) >= MIN_QUOTE_WORDS and sum(map(len, parts)) >= MIN_QUOTE_CHARS


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
