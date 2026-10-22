from casefile.ingest import Document, chunk_document
from casefile.people import Cast, Person, parse_name
from casefile.timeline import Event, deduplicate, locate_quote, normalize_date


def _cast(records):
    """records: list of (chunk_id, full_name, aliases)."""
    cast = Cast()
    for i, (chunk, full, aliases) in enumerate(records):
        cast.add_person(chunk, full, aliases, "", (i, 0, ""))
    people = cast.finalize()
    return cast, sorted(p.label for p in people)


def test_unique_first_names_are_used_alone():
    _, labels = _cast([(1, "Sarah Kim", []), (2, "Tom Reyes", [])])
    assert labels == ["Sarah", "Tom"]


def test_shared_first_name_adds_surname_only_for_those_people():
    _, labels = _cast([(1, "Sarah Kim", []), (2, "Sarah Lee", []), (3, "Tom Reyes", [])])
    assert labels == ["Sarah Kim", "Sarah Lee", "Tom"]


def test_identical_full_names_in_one_record_are_one_person():
    # Same name across files is assumed to be the same person.
    _, labels = _cast([(1, "John Smith", []), (2, "John Smith", [])])
    assert labels == ["John"]


def test_partial_names_attach_only_when_unambiguous():
    _, labels = _cast([(1, "Sarah Kim", []), (2, "Dr. Kim", []), (3, "Sarah", [])])
    assert labels == ["Sarah"]
    _, labels = _cast([(1, "Sarah Kim", []), (2, "Sarah Lee", []), (3, "Sarah", [])])
    assert labels == ["Sarah (unclear)", "Sarah Kim", "Sarah Lee"]


def test_short_alias_is_scoped_to_its_chunk():
    # "Sarah" means Sarah Kim in chunk 1 and Sarah Lee in chunk 2; they must stay two people.
    cast, labels = _cast([(1, "Sarah Kim", ["Sarah"]), (2, "Sarah Lee", ["Sarah"])])
    assert labels == ["Sarah Kim", "Sarah Lee"]
    assert cast.resolve(1, "Sarah").label == "Sarah Kim"
    assert cast.resolve(2, "Sarah").label == "Sarah Lee"


def test_nickname_and_comma_forms_merge():
    _, labels = _cast([(1, "Robert Smith", []), (2, "Bob Smith", []), (3, "Smith, Robert", []), (4, "Ann Lee", [])])
    assert labels == ["Ann", "Robert"]


def test_ambiguous_nickname_does_not_merge():
    _, labels = _cast([(1, "Christopher Lee", []), (2, "Christina Lee", []), (3, "Chris Lee", [])])
    assert len(labels) == 3


def test_surname_only_keeps_title():
    _, labels = _cast([(1, "Dr. Patel", [])])
    assert labels == ["Dr. Patel"]


def test_relabel_is_case_sensitive_and_whole_word():
    cast = Cast()
    cast.add_person(1, "Will Turner", ["Will"], "", (0, 0, ""))
    cast.add_person(1, "Willa Stone", [], "", (0, 0, ""))
    cast.finalize()
    assert cast.relabel(1, "Will Turner said Willa Stone will pay; Will agreed.") == "Will said Willa will pay; Will agreed."


def test_quote_location_tolerates_formatting_but_not_invention():
    text = "Line one.\nOn 4 March the  tenant said: “I won’t pay until\nthe boiler is fixed.”\n"
    assert locate_quote("I won't pay until the boiler is fixed", text) == text.index("I won")
    assert locate_quote("I won't pay ... boiler is fixed", text) == text.index("I won")
    assert locate_quote("I will never pay the landlord anything", text) is None
    assert locate_quote("", text) is None


def test_dates_are_normalised_only_when_unambiguous():
    assert normalize_date("2023-04-05") == "2023-04-05"
    assert normalize_date("2023-04-05T10:30") == "2023-04-05"
    assert normalize_date("April 5th, 2023") == "2023-04-05"
    assert normalize_date("5 Apr 2023") == "2023-04-05"
    assert normalize_date("2023-13") == ""
    assert normalize_date("03/04/2023") == ""  # March or April? Not guessed.
    assert normalize_date("") == ""


def test_chunks_cover_text_with_overlap_and_cite_lines():
    text = "".join(f"line {i}\n" for i in range(1, 400))
    doc = Document("a.txt", text)
    chunks = chunk_document(doc, 0, size=500, overlap=100)
    covered = set()
    for c in chunks:
        assert text[c.start : c.start + len(c.text)] == c.text
        covered.update(range(c.start, c.start + len(c.text)))
    assert covered == set(range(len(text)))
    assert all(b.start < a.start + len(a.text) for a, b in zip(chunks, chunks[1:]))  # overlap
    assert doc.location(text.index("line 120\n")) == "line 120"


def test_descriptions_are_not_surnames():
    assert parse_name("Sarah at Northgate").key == ("1", "sarah")
    assert parse_name("Bob's agent") is None
    assert parse_name("Ludwig van Beethoven").key == ("F", "ludwig", "beethoven")


def _event(date, summary, quote, doc, actors=()):
    return Event(date, date, summary, quote, True, f"doc{doc}", doc, 0, list(actors))


def test_copy_quoted_in_a_reply_is_one_event_and_keeps_the_date():
    original = _event("", "Sarah reported a leak to Sarah Lee.", "Water started coming through the ceiling", 0)
    quoted_copy = _event("2023-01-12", "Sarah reported a leak on 12 January.", "Water started coming through the ceiling", 1)
    assert [e.date for e in deduplicate([original, quoted_copy])] == ["2023-01-12"]


def test_one_sentence_can_support_two_different_events():
    quote = "Received a letter from Robert ending the tenancy. Will thinks it is because we complained."
    a = _event("2023-04-28", "Robert sent a letter ending the tenancy.", quote, 0, [Person(1)])
    b = _event("2023-04-28", "Will said the eviction was retaliation for the complaint.", quote, 0, [Person(2)])
    assert len(deduplicate([a, b])) == 2


def test_similar_sentences_with_different_evidence_are_different_events():
    sarah = Person(1)
    a = _event("2018-02-06", "Sarah phoned James about the invoice.", "On 6 Feb Sarah phoned James about it", 0, [sarah])
    b = _event("2018-02-06", "Sarah paid James for the invoice.", "Later that day Sarah paid James in cash", 0, [sarah])
    assert len(deduplicate([a, b])) == 2


def test_overlapping_parts_quoting_more_or_less_of_a_passage_are_one_event():
    sarah = Person(1)
    a = _event("2023-01-12", "Sarah reported a leak.", "Water started coming through the bathroom ceiling", 0, [sarah])
    b = _event("2023-01-12", "Sarah said the ceiling leaked.", "Water started coming through the bathroom ceiling at 14 Harbour Road", 0, [sarah])
    assert len(deduplicate([a, b])) == 1


def test_a_duplicate_keeps_the_highest_importance_any_copy_was_given():
    sarah = Person(1)
    # The kept copy (verified, dated) was judged standard; the copy in a reply chain was judged major.
    kept = _event("2023-04-26", "Robert served notice.", "I require possession of Flat 2", 0, [sarah])
    copy = _event("2023-04-26", "Robert served notice.", "I require possession of Flat 2", 1, [sarah])
    copy.importance = "major"
    [result] = deduplicate([kept, copy])
    assert result.doc_index == 0 and result.importance == "major"
