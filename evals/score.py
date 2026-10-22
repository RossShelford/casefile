"""Scores one case's report against its gold labels. Pure functions: no model, no files.

Gold events are matched one-to-one to the report's events (the verified ones: what a reader
sees in the main log). A pair can match when
  - the dates agree: equal, or one is a less precise form of the other ("2023-04" and
    "2023-04-05"); an undated gold event only matches an undated report event; and
  - enough of the gold keywords appear in the report event's summary or quote: at least half
    of them, rounded up. A keyword may list alternatives separated by "|" ("evict|section 21");
    each is a case-insensitive substring, so "evict" also finds "eviction".
Among the pairs that can match, the most gold events are paired (maximum bipartite matching),
preferring better keyword and date agreement. Report events left over count against precision:
an extra, duplicated or misdated event is noise for the reader.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from casefile.taxonomy import RANK

KEYWORD_SHARE = 0.5


def keyword_hits(keywords: list[str], text: str) -> int:
    """How many keywords (each "a|b|c" = any of a, b, c) occur in the text."""
    text = text.casefold()
    return sum(1 for k in keywords if any(alt.strip() and alt.strip().casefold() in text for alt in k.split("|")))


def dates_compatible(gold: str, predicted: str) -> bool:
    if not gold or not predicted:
        return gold == predicted
    return gold.startswith(predicted) or predicted.startswith(gold)


def match_events(gold: list[dict], predicted: list[dict]) -> list[tuple[int, int]]:
    """(gold index, predicted index) pairs: as many as possible, each event used once."""
    candidates: list[list[int]] = []
    for g in gold:
        keywords = g["keywords"]
        need = max(1, math.ceil(len(keywords) * KEYWORD_SHARE))
        scored = []
        for pi, p in enumerate(predicted):
            if not dates_compatible(g.get("date", ""), p.get("date", "")):
                continue
            hits = keyword_hits(keywords, f"{p.get('summary', '')} {p.get('quote', '')}")
            if hits >= need:
                scored.append((-hits, g.get("date", "") != p.get("date", ""), pi))
        candidates.append([pi for *_, pi in sorted(scored)])

    owner: dict[int, int] = {}  # predicted index -> gold index

    def assign(gi: int, visited: set[int]) -> bool:
        # Kuhn's augmenting path: take a free candidate, or move its current owner elsewhere.
        for pi in candidates[gi]:
            if pi in visited:
                continue
            visited.add(pi)
            if pi not in owner or assign(owner[pi], visited):
                owner[pi] = gi
                return True
        return False

    # Gold events with the fewest candidates go first, so the greedy first pass leaves
    # fewer conflicts for the augmenting paths to undo.
    for gi in sorted(range(len(gold)), key=lambda i: (len(candidates[i]), i)):
        assign(gi, set())
    return sorted((gi, pi) for pi, gi in owner.items())


def _person_forms(person: dict) -> set[str]:
    return {s.casefold() for s in [person["name"], *person.get("also", []), *person.get("labels", [])]}


@dataclass
class Score:
    gold_events: int = 0
    predicted_events: int = 0
    matched: int = 0
    exact_dates: int = 0
    importance_exact: int = 0
    importance_within_one: int = 0
    people: int = 0
    people_found: int = 0
    people_split: int = 0  # one gold person shown as several people
    people_merged: int = 0  # several gold people shown as one
    labels_ok: int = 0  # found exactly once, under an acceptable label
    unclear_people: int = 0  # bare first names the report couldn't pin to anyone
    unverified: int = 0  # events whose quote wasn't found: shown separately, not scored
    failed_parts: int = 0
    missed: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    label_problems: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.matched / self.gold_events if self.gold_events else 0.0

    @property
    def precision(self) -> float:
        return self.matched / self.predicted_events if self.predicted_events else 0.0

    def __add__(self, other: Score) -> Score:
        merged = Score()
        for name, value in asdict(self).items():
            setattr(merged, name, value + getattr(other, name))
        return merged

    def to_dict(self) -> dict:
        return {**asdict(self), "recall": self.recall, "precision": self.precision}


def score_case(gold: dict, events: list[dict], people: list[dict], *, unverified: int = 0, failed_parts: int = 0) -> Score:
    """gold: {"events": [{id, date, importance, keywords, what}], "people": [{name, also, labels}]}
    events: the report's verified events as {date, summary, quote, importance}
    people: the report's people as {label, full_name, unresolved}"""
    s = Score(unverified=unverified, failed_parts=failed_parts)
    gold_events = gold["events"]
    s.gold_events, s.predicted_events = len(gold_events), len(events)
    pairs = match_events(gold_events, events)
    s.matched = len(pairs)
    for gi, pi in pairs:
        g, p = gold_events[gi], events[pi]
        s.exact_dates += g.get("date", "") == p.get("date", "")
        diff = abs(RANK[g["importance"]] - RANK.get(p.get("importance", ""), RANK["standard"]))
        s.importance_exact += diff == 0
        s.importance_within_one += diff <= 1
    used_gold = {gi for gi, _ in pairs}
    used_pred = {pi for _, pi in pairs}
    s.missed = [f"{g['id']} {g.get('date') or 'undated'}: {g.get('what', '')}" for i, g in enumerate(gold_events) if i not in used_gold]
    s.extra = [f"{p.get('date') or 'undated'}: {p.get('summary', '')}" for i, p in enumerate(events) if i not in used_pred]

    s.people = len(gold["people"])
    s.unclear_people = sum(1 for p in people if p.get("unresolved"))
    owners: dict[int, list[str]] = {}
    for g in gold["people"]:
        forms = _person_forms(g)
        hits = [i for i, p in enumerate(people) if p.get("full_name", "").casefold() in forms or p.get("label", "").casefold() in forms]
        for i in hits:
            owners.setdefault(i, []).append(g["name"])
        if not hits:
            s.label_problems.append(f"{g['name']}: not in the who's-who")
            continue
        s.people_found += 1
        if len(hits) > 1:
            s.people_split += 1
            s.label_problems.append(f"{g['name']}: split into " + ", ".join(repr(people[i].get("label", "")) for i in hits))
            continue
        label = people[hits[0]].get("label", "")
        if label.casefold() in {x.casefold() for x in g.get("labels", [g["name"]])}:
            s.labels_ok += 1
        else:
            s.label_problems.append(f"{g['name']}: shown as {label!r}")
    for i, names in owners.items():
        if len(names) > 1:
            s.people_merged += 1
            s.label_problems.append(f"{' and '.join(names)}: merged into {people[i].get('label', '')!r}")
    return s


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else f"{n}/{d}"


def format_score(name: str, s: Score, details: bool = True) -> str:
    lines = [
        f"{name}",
        f"  events      recall {_pct(s.matched, s.gold_events)}, precision {_pct(s.matched, s.predicted_events)},"
        f" exact date {_pct(s.exact_dates, s.matched)}",
        f"  importance  exact {_pct(s.importance_exact, s.matched)}, within one level {_pct(s.importance_within_one, s.matched)}",
        f"  people      found {_pct(s.people_found, s.people)}, right label {_pct(s.labels_ok, s.people)},"
        f" split {s.people_split}, merged {s.people_merged}, unclear {s.unclear_people}",
        f"  also        unverified events {s.unverified}, failed parts {s.failed_parts}",
    ]
    if details:
        lines += [f"  missed      {m}" for m in s.missed]
        lines += [f"  extra       {e}" for e in s.extra]
        lines += [f"  people      {p}" for p in s.label_problems]
    return "\n".join(lines)
