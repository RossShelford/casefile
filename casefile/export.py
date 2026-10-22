"""Report -> plain JSON-able dict, for the web page and API clients."""

from __future__ import annotations

from collections import Counter

from .pipeline import Report
from .taxonomy import IMPORTANCE, KIND_ICON
from .timeline import Event, date_label


def _event(ev: Event) -> dict:
    return {
        "date": ev.date,
        "date_label": date_label(ev.date) if ev.date else (ev.date_as_written or ""),
        "summary": ev.summary,
        "quote": ev.quote,
        "source": ev.source,
        "kind": ev.kind,
        "icon": KIND_ICON[ev.kind],
        "importance": ev.importance,
        "people": [p.label for p in ev.actors],
    }


def report_to_dict(report: Report) -> dict:
    dated = [e for e in report.events if e.date]
    counts = Counter(e.importance for e in report.events)
    return {
        "title": report.title,
        "folder": report.folder,
        "model": report.model,
        "generated": report.generated.isoformat(timespec="seconds"),
        "period": [dated[0].date, dated[-1].date] if dated else None,
        "counts": {level: counts.get(level, 0) for level in IMPORTANCE},
        "people": [
            {
                "label": p.label,
                "full_name": p.full_name,
                "roles": p.top_roles(),
                "events": p.events,
                "active": [p.first_event, p.last_event] if p.first_event else None,
                "first_seen": p.first_seen[2] if p.first_seen else "",
            }
            for p in report.people
        ],
        "events": [_event(e) for e in report.events],
        "unverified": [_event(e) for e in report.unverified],
        "sources": [
            {"path": d.path, "status": d.status, "events": report.events_per_doc.get(d.path, 0)}
            for d in sorted(report.documents, key=lambda d: d.path)
        ],
        "failures": [{"where": w, "error": e} for w, e in report.failures],
    }
