"""How events are classified. Shared by the model prompt, the PDF and the web page so the
same words, colours and icons mean the same thing everywhere."""

# Lowest to highest. Colours: grey, black, amber, red. There is deliberately no green:
# "nothing important happened" is shown by grey, not by a reassuring colour.
IMPORTANCE = ("minor", "standard", "notable", "major")
RANK = {level: i for i, level in enumerate(IMPORTANCE)}

IMPORTANCE_LABEL = {"minor": "Minor", "standard": "Standard", "notable": "Notable", "major": "Major"}

IMPORTANCE_COLOR = {
    "minor": "#8B9099",  # grey: background, routine
    "standard": "#000000",  # black: an ordinary step
    "notable": "#F2A900",  # amber: worth noticing
    "major": "#D0021B",  # red: changed the course of things
}

# What each level means, for readers (the PDF legend and the app's key). The model's own,
# stricter instructions are IMPORTANCE_RUBRIC below.
IMPORTANCE_MEANING = {
    "major": "Changed the course of things: legal steps, formal notices, official findings, harm, large sums.",
    "notable": "A meaningful step: complaints, inspections, agreements, refusals, repairs, payments.",
    "standard": "An ordinary action or message.",
    "minor": "Routine background: acknowledgements, scheduling, chasing.",
}

IMPORTANCE_RUBRIC = """importance: how much the event matters to the overall story. Decide by what happened, not by tone.
  - "major": changed the course of things. Always "major":
    any notice that ends or threatens to end a tenancy, job, contract or service (eviction notice,
    notice to quit, possession notice, dismissal, termination); a court claim, hearing or judgment;
    an official finding, enforcement notice or penalty; injury, serious harm or danger; a large sum
    of money; an arrest or a report to police.
  - "notable": a meaningful step: a formal complaint or request; an inspection or assessment; an agreement,
    refusal or decision; a repair or fix carried out; a deadline set or missed; a payment.
  - "standard": an ordinary action or message that moves things along.
  - "minor": routine background: acknowledgements, scheduling, chasing, "nothing new", pleasantries.
  Most events are "standard". Only the cases listed under "major" are major."""

# Event kind -> icon file in assets/icons (Lucide, ISC licence).
KIND_ICON = {
    "message": "mail",  # email, letter, text, post
    "call": "phone",
    "meeting": "users",  # meeting, visit, hearing attended
    "payment": "banknote",
    "legal": "scale",  # notices, claims, court, contracts
    "inspection": "clipboard-check",  # inspections, assessments, reports, findings
    "incident": "zap",  # damage, accident, failure, harm
    "repair": "wrench",  # work carried out
    "decision": "handshake",  # agreements, refusals, decisions
    "other": "circle-dot",
}
KINDS = tuple(KIND_ICON)
