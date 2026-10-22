"""Folder in, Report out: read -> chunk -> extract (model) -> resolve people -> build timeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .extract import ExtractionError, Extractor
from .ingest import Chunk, Document, chunk_document, scan_folder
from .people import Cast, Person
from .timeline import Event, deduplicate, locate_quote, normalize_date


@dataclass
class Report:
    title: str
    folder: str
    model: str
    generated: datetime
    documents: list[Document]
    people: list[Person]
    events: list[Event]  # quote verified, in date order (undated last)
    unverified: list[Event]  # quote not found in the source: shown separately, never mixed in
    failures: list[tuple[str, str]] = field(default_factory=list)  # (source, error)
    events_per_doc: dict[str, int] = field(default_factory=dict)
    cache_keys: set[str] = field(default_factory=set)  # the parts cache entries this report used


class Cancelled(Exception):
    """The caller asked the run to stop. Finished parts stay cached."""


class FolderNotFound(Exception):
    """The folder to read isn't there (moved, renamed, or on a drive that's not connected)."""


Progress = Callable[[int, int, str], None]
ShouldStop = Callable[[], bool]


def build_report(
    folder: Path,
    extractor: Extractor,
    *,
    title: str,
    chunk_size: int,
    overlap: int,
    jobs: int,
    progress: Progress,
    not_sent: Sequence[str] = (),
    should_stop: ShouldStop | None = None,
) -> Report:
    # Without this check a missing folder reads as an empty one, giving a "done" report with
    # no events instead of telling the user the folder has gone.
    if not folder.is_dir():
        raise FolderNotFound(
            f"The folder can't be found: {folder}. It may have been moved or renamed, "
            "or it's on a drive that isn't connected."
        )
    stop = should_stop or (lambda: False)
    docs = scan_folder(folder)
    if stop():  # reading a large folder can take a while
        raise Cancelled()
    chunks: list[Chunk] = []
    for i, doc in enumerate(docs):
        if doc.status == "ok":
            chunks.extend(chunk_document(doc, i, chunk_size, overlap))
    parts = {}
    for c in chunks:
        parts[c.doc_index] = parts.get(c.doc_index, 0) + 1

    def run(chunk: Chunk) -> dict | ExtractionError:
        if stop():  # don't send another part once the caller has asked to stop
            raise Cancelled()
        doc = docs[chunk.doc_index]
        try:
            return extractor.extract(doc.path, chunk.index + 1, parts[chunk.doc_index], chunk.text)
        except ExtractionError as exc:
            return exc

    results: list[dict | ExtractionError] = []
    progress(0, len(chunks), "")  # the total is known now; the first part can take a minute
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        # map() keeps order; a ModelServerError, Cancelled (or Ctrl-C) propagates and stops the run.
        for n, result in enumerate(pool.map(run, chunks), 1):
            results.append(result)
            chunk = chunks[n - 1]
            progress(n, len(chunks), f"{docs[chunk.doc_index].path} ({chunk.index + 1}/{parts[chunk.doc_index]})")
            if stop():  # checked between parts: the part in flight is finished and cached first
                raise Cancelled()
    finally:
        # Finished chunks are already cached, so a rerun resumes where this one stopped.
        pool.shutdown(wait=True, cancel_futures=True)

    def cite(doc_index: int, offset: int) -> str:
        doc = docs[doc_index]
        return f"{doc.path}, {doc.location(offset)}"

    # Pass 1: everyone mentioned anywhere. Names are only assigned once all are known.
    cast = Cast()
    failures: list[tuple[str, str]] = []
    for chunk, result in zip(chunks, results):
        cid = (chunk.doc_index, chunk.index)
        if isinstance(result, ExtractionError):
            failures.append((cite(chunk.doc_index, chunk.start), str(result)))
            continue
        lowered = chunk.text.casefold()

        def seen(*names: str) -> tuple:
            """Position of the earliest of these names in the chunk (chunk start if absent)."""
            hits = [i for n in names if n and (i := lowered.find(n.casefold())) != -1]
            offset = chunk.start + min(hits, default=0)
            return (chunk.doc_index, offset, cite(chunk.doc_index, offset))

        for p in result["people"]:
            cast.add_person(cid, p["full_name"], p["aliases"], p["role"], seen(p["full_name"], *p["aliases"]))
        for e in result["events"]:
            for actor in e["actors"]:
                cast.add_actor(cid, actor, seen(actor))
    people = cast.finalize()

    # Pass 2: events, checked against the source text and rewritten with final names.
    events: list[Event] = []
    for chunk, result in zip(chunks, results):
        if isinstance(result, ExtractionError):
            continue
        cid = (chunk.doc_index, chunk.index)
        for e in result["events"]:
            found = locate_quote(e["quote"], chunk.text)
            offset = chunk.start + (found or 0)
            actors: list[Person] = []
            for name in e["actors"]:
                person = cast.resolve(cid, name)
                if person and person not in actors:
                    actors.append(person)
            events.append(
                Event(
                    date=normalize_date(e["date"]),
                    date_as_written=e["date"],
                    summary=cast.relabel(cid, e["summary"]),
                    quote=e["quote"],
                    verified=found is not None,
                    source=cite(chunk.doc_index, offset),
                    doc_index=chunk.doc_index,
                    offset=offset,
                    actors=actors,
                    kind=e["kind"],
                    importance=e["importance"],
                )
            )
    events = deduplicate(events)
    verified = [e for e in events if e.verified]
    unverified = [e for e in events if not e.verified]
    # Dated events in date order, then undated ones in document order.
    verified.sort(key=lambda e: (not e.date, e.sort_key))
    unverified.sort(key=lambda e: (e.doc_index, e.offset))

    events_per_doc: dict[str, int] = {}
    for ev in verified:
        for person in ev.actors:
            person.events += 1
            if ev.date:  # verified is in date order, dated first
                person.first_event = person.first_event or ev.date
                person.last_event = ev.date
        path = docs[ev.doc_index].path
        events_per_doc[path] = events_per_doc.get(path, 0) + 1
    # Most involved first: that is who a reader needs to know.
    people.sort(key=lambda p: (-p.events, -p.mentions, p.first_seen))

    # Files the caller chose not to send (e.g. photos filtered out by the web page before
    # upload) are still listed, so the Sources table accounts for the whole folder.
    # Appended, not sorted in: Event.doc_index points into this list.
    docs = docs + [Document(path, status="skipped: unsupported file type") for path in not_sent]

    return Report(
        title=title,
        folder=folder.name,
        model=extractor.config.model,
        generated=datetime.now(),
        documents=docs,
        people=people,
        events=verified,
        unverified=unverified,
        failures=failures,
        events_per_doc=events_per_doc,
        cache_keys=set(extractor.keys_used),
    )
