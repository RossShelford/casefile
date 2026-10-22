"""Unit tests for the eval scoring, plus checks that the gold labels are well-formed.
No model needed; collected by the normal `pytest` run."""

import json
import math
import re
from pathlib import Path

import pytest

from casefile.taxonomy import IMPORTANCE

from .score import Score, dates_compatible, format_score, keyword_hits, match_events, score_case

HERE = Path(__file__).resolve().parent
GOLD_FILES = sorted((HERE / "gold").glob("*.json"))


def ev(date, summary, quote="", importance="standard"):
    return {"date": date, "summary": summary, "quote": quote, "importance": importance}


def test_keywords_match_alternatives_and_word_stems_case_insensitively():
    assert keyword_hits(["evict|section 21", "notice"], "Section 21 NOTICE served") == 2
    assert keyword_hits(["evict"], "an eviction") == 1
    assert keyword_hits(["mould", "mattress"], "damp in the hall") == 0


def test_dates_match_when_one_is_a_less_precise_form_of_the_other():
    assert dates_compatible("2023-04-05", "2023-04-05")
    assert dates_compatible("2023-04", "2023-04-05")
    assert dates_compatible("2023-04-05", "2023")
    assert not dates_compatible("2023-04-05", "2023-04-06")
    assert not dates_compatible("2023-04-05", "")
    assert dates_compatible("", "")


def test_a_match_needs_at_least_half_the_keywords():
    gold = [{"date": "2023-01-10", "keywords": ["damp", "phone", "bedroom"]}]
    assert match_events(gold, [ev("2023-01-10", "damp reported")]) == []
    assert match_events(gold, [ev("2023-01-10", "Damp reported", "rang on the phone")]) == [(0, 0)]


def test_each_event_is_used_once_and_as_many_gold_events_as_possible_are_matched():
    gold = [
        {"date": "2023-03-01", "keywords": ["notice"]},  # matches both predictions
        {"date": "2023-03-01", "keywords": ["court"]},  # matches only the first
    ]
    predicted = [ev("2023-03-01", "court notice"), ev("2023-03-01", "notice served")]
    assert match_events(gold, predicted) == [(0, 1), (1, 0)]
    # A duplicate of one gold event is left over (it lowers precision).
    assert match_events(gold[:1], predicted) == [(0, 0)]


def test_score_case_counts_events_importance_and_people():
    gold = {
        "events": [
            {"id": "A", "date": "2023-01-10", "importance": "notable", "keywords": ["damp", "phone"], "what": "damp reported"},
            {"id": "B", "date": "2023-06-06", "importance": "major", "keywords": ["inspection", "hazard"], "what": "inspection"},
            {"id": "C", "date": "2023-07-04", "importance": "major", "keywords": ["section 21"], "what": "eviction notice"},
        ],
        "people": [
            {"name": "Sarah Kim", "labels": ["Sarah", "Sarah Kim"]},
            {"name": "Tom Price", "also": ["T. Price"], "labels": ["Tom", "Tom Price"]},
            {"name": "Leanne Moss", "labels": ["Leanne"]},
            {"name": "Graham Whitfield", "also": ["Mr Whitfield"], "labels": ["Graham"]},
        ],
    }
    events = [
        ev("2023-01-10", "Sarah reports damp by phone", importance="notable"),  # A, exact
        ev("2023-06", "Council inspection finds a hazard", importance="standard"),  # B, two levels off
        ev("2023-05-01", "Tom sends a newsletter", importance="minor"),  # extra
    ]
    people = [
        {"label": "Sarah", "full_name": "Sarah Kim", "unresolved": False},
        {"label": "Tom", "full_name": "Tom Price", "unresolved": False},
        {"label": "Price", "full_name": "T. Price", "unresolved": False},  # Tom split in two
        {"label": "Leanne Moss", "full_name": "Leanne Moss", "unresolved": False},  # wrong label
        {"label": "Sarah (unclear)", "full_name": "", "unresolved": True},
    ]
    s = score_case(gold, events, people, unverified=2, failed_parts=1)
    assert (s.gold_events, s.predicted_events, s.matched) == (3, 3, 2)
    assert s.recall == pytest.approx(2 / 3) and s.precision == pytest.approx(2 / 3)
    assert s.exact_dates == 1
    assert (s.importance_exact, s.importance_within_one) == (1, 1)
    assert (s.people, s.people_found, s.people_split, s.labels_ok, s.unclear_people) == (4, 3, 1, 1, 1)
    assert s.people_merged == 0
    assert s.missed == ["C 2023-07-04: eviction notice"]
    assert s.extra == ["2023-05-01: Tom sends a newsletter"]
    assert any("Graham Whitfield: not in the who's-who" == p for p in s.label_problems)
    assert any("shown as 'Leanne Moss'" in p for p in s.label_problems)
    assert (s.unverified, s.failed_parts) == (2, 1)
    text = format_score("case", s)
    assert "recall 2/3 (67%)" in text and "missed      C 2023-07-04" in text


def test_two_gold_people_shown_as_one_count_as_merged():
    gold = {"events": [], "people": [{"name": "Sarah Kim", "labels": ["Sarah"]}, {"name": "Sarah Lee", "labels": ["Sarah"]}]}
    s = score_case(gold, [], [{"label": "Sarah", "full_name": "Sarah Kim", "unresolved": False}])
    assert s.people_merged == 1


def test_scores_add_up_across_cases():
    a = Score(gold_events=3, predicted_events=4, matched=2, missed=["x"])
    b = Score(gold_events=5, predicted_events=5, matched=5)
    total = sum([a, b], Score())
    assert (total.gold_events, total.predicted_events, total.matched, total.missed) == (8, 9, 7, ["x"])
    assert total.recall == pytest.approx(7 / 8)
    assert Score().recall == 0.0 and Score().precision == 0.0


@pytest.mark.parametrize("path", GOLD_FILES, ids=lambda p: p.stem)
def test_gold_labels_are_well_formed(path):
    gold = json.loads(path.read_text(encoding="utf-8"))
    case = HERE / "cases" / path.stem
    assert case.is_dir(), f"no documents for {path.stem}"
    ids = [e["id"] for e in gold["events"]]
    assert len(ids) == len(set(ids))
    for e in gold["events"]:
        assert re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", e["date"]), e
        assert e["importance"] in IMPORTANCE, e
        assert e["keywords"] and all(k.strip() for k in e["keywords"]), e
        assert e["what"], e
    names = [p["name"] for p in gold["people"]]
    assert len(names) == len(set(names))
    for p in gold["people"]:
        assert p["labels"], p


def test_the_set_is_big_enough_to_mean_something():
    assert len(GOLD_FILES) >= 3
    assert sum(len(json.loads(p.read_text(encoding="utf-8"))["events"]) for p in GOLD_FILES) >= 25
    assert {d.name for d in (HERE / "cases").iterdir() if d.is_dir()} == {p.stem for p in GOLD_FILES}


@pytest.mark.parametrize("path", GOLD_FILES, ids=lambda p: p.stem)
def test_every_gold_event_can_be_matched_from_the_documents(path):
    """A model quoting the source can only match a gold event if enough of its keywords
    occur in the case's documents. Catches typos in keywords and edits to the documents."""
    from casefile.ingest import scan_folder

    gold = json.loads(path.read_text(encoding="utf-8"))
    docs = scan_folder(HERE / "cases" / path.stem)
    assert docs and all(d.status == "ok" for d in docs), [(d.path, d.status) for d in docs if d.status != "ok"]
    text = "\n".join(d.text for d in docs)
    for e in gold["events"]:
        need = max(1, math.ceil(len(e["keywords"]) * 0.5))
        assert keyword_hits(e["keywords"], text) >= need, e["id"]
