"""Ask a local model to pull people and events out of one chunk, as strict JSON.

The model only *extracts*. Merging people, naming them, ordering, de-duplicating and
checking quotes all happen in plain code afterwards, so results stay consistent across
thousands of chunks and every claim can be traced to its source.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .taxonomy import IMPORTANCE, IMPORTANCE_RUBRIC, KINDS

PROMPT_VERSION = "4"  # bump when the prompt or schema changes; invalidates the cache

SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "role": {"type": "string"},
                },
                "required": ["full_name", "aliases", "role"],
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "importance": {"type": "string", "enum": list(IMPORTANCE)},
                    "actors": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["date", "kind", "importance", "actors", "summary", "quote"],
            },
        },
    },
    "required": ["people", "events"],
}

SYSTEM = (
    "You turn documents into a factual activity log. Use only what the text states. "
    "Never guess names, dates or motives. Reply with JSON only."
)

INSTRUCTIONS = (
    """From the document excerpt below, extract:

people: every named individual person (not companies). For each:
- full_name: the most complete form of their name found in the excerpt, without titles like Mr/Dr.
- aliases: other ways the excerpt refers to them (nickname, "Mr. Smith", "Dr. Patel", email address).
- role: who they are in a few words if the excerpt says (e.g. "landlord", "Acme CFO"), else "".

events: things that happened - actions, meetings, calls, messages, decisions, payments, incidents. For each:
- date: when it happened, as YYYY-MM-DD, or YYYY-MM / YYYY if only that is stated, or "" if no date is given.
  An email or letter carrying its own date (e.g. a Date: header) was sent on that date.
- kind: one of message, call, meeting, payment, legal, inspection, incident, repair, decision, other.
- """
    + IMPORTANCE_RUBRIC.replace("{", "{{").replace("}", "}}")
    + """
- actors: full_name of each person involved, exactly as written in people.
- summary: one short past-tense sentence in plain English that names people by full_name.
- quote: 5-30 words copied EXACTLY, character for character, from the excerpt that show this happened.

Skip email signatures, letterheads, page headers and footers, and legal boilerplate.
If nothing happened in the excerpt, return an empty events list.

Document: {path} (part {part} of {parts})
<<<
{text}
>>>"""
)


@dataclass
class ModelConfig:
    model: str
    base_url: str  # e.g. http://localhost:11434
    api: str  # "ollama" (native, lets us set the context window) or "openai" (any compatible server)
    num_ctx: int
    timeout: float


class ModelServerError(RuntimeError):
    """The model server is missing or rejecting requests. Stops the run."""


class ExtractionError(RuntimeError):
    """One chunk produced no usable answer. Recorded; the run continues."""


def check_model(config: ModelConfig) -> tuple[str | None, str]:
    """Is the server up and the model installed?

    Returns (problem, plain-English message); problem is None when ready, "server" when the
    model server can't be reached, "model" when the model isn't installed."""
    base = config.base_url.rstrip("/")
    url = f"{base}/api/tags" if config.api == "ollama" else f"{base}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        hint = " Start it with: ollama serve" if config.api == "ollama" else ""
        return "server", f"Can't reach the AI model server at {base}.{hint} ({exc})"
    if config.api == "ollama":
        names = {m.get("name", "") for m in payload.get("models", [])}
        installed = config.model in names or f"{config.model}:latest" in names
        fix = f" Install it with: ollama pull {config.model}"
    else:
        installed = config.model in {m.get("id", "") for m in payload.get("data", [])}
        fix = " Load it in your model server."
    if not installed:
        return "model", f"The model {config.model} isn't installed.{fix}"
    return None, f"{config.model} is ready."


def pull_model(config: ModelConfig, on_progress) -> None:
    """Download the model through Ollama, calling on_progress(completed_bytes, total_bytes).

    Ollama streams one JSON line per update, per layer; the totals here add up all layers."""
    if config.api != "ollama":
        raise ModelServerError("Only Ollama can download models. Load the model in your model server instead.")
    url = f"{config.base_url.rstrip('/')}/api/pull"
    body = json.dumps({"model": config.model, "stream": True}).encode()
    layers: dict[str, tuple[int, int]] = {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"})) as resp:
            for line in resp:
                if not line.strip():
                    continue
                update = json.loads(line)
                if "error" in update:
                    raise ModelServerError(update["error"])
                if update.get("digest") and update.get("total"):
                    layers[update["digest"]] = (update.get("completed", 0), update["total"])
                    on_progress(sum(c for c, _ in layers.values()), sum(t for _, t in layers.values()))
                if update.get("status") == "success":
                    return
    except urllib.error.URLError as exc:
        raise ModelServerError(f"Can't reach the AI model server: {exc.reason}") from exc
    raise ModelServerError("The download stopped before it finished. Try again.")


class Extractor:
    def __init__(self, config: ModelConfig, cache_dir: Path):
        self.config = config
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def extract(self, path: str, part: int, parts: int, text: str) -> dict:
        prompt = INSTRUCTIONS.format(path=path, part=part, parts=parts, text=text)
        key = hashlib.sha256(
            "\0".join((PROMPT_VERSION, self.config.model, SYSTEM, prompt)).encode()
        ).hexdigest()
        cached = self.cache_dir / f"{key}.json"
        if cached.exists():
            return json.loads(cached.read_text())
        result = _clean(self._call(prompt))
        # Unique temp name: with --jobs > 1, two identical files can finish at the same moment.
        tmp = self.cache_dir / f"{key}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(result))
        tmp.replace(cached)  # atomic: an interrupted run never leaves a half-written cache file
        return result

    def _call(self, prompt: str) -> dict:
        c = self.config
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        last_error: Exception | None = None
        # Second attempt uses another seed: small models occasionally emit broken JSON,
        # and repeating the identical seeded request would reproduce it.
        for seed in (0, 1):
            if c.api == "ollama":
                url = f"{c.base_url.rstrip('/')}/api/chat"
                body = {
                    "model": c.model,
                    "messages": messages,
                    "stream": False,
                    "format": SCHEMA,
                    # Ollama silently truncates prompts longer than num_ctx; set it explicitly.
                    "options": {"temperature": 0, "seed": seed, "num_ctx": c.num_ctx},
                }
            else:
                url = f"{c.base_url.rstrip('/')}/v1/chat/completions"
                body = {
                    "model": c.model,
                    "messages": messages,
                    "temperature": 0,
                    "seed": seed,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "extraction", "schema": SCHEMA, "strict": True},
                    },
                }
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=c.timeout) as resp:
                    payload = json.load(resp)
                content = (
                    payload["message"]["content"]
                    if c.api == "ollama"
                    else payload["choices"][0]["message"]["content"]
                )
                return json.loads(content)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                raise ModelServerError(f"{url} returned HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    last_error = exc
                    continue
                raise ModelServerError(f"cannot reach model server at {c.base_url}: {exc.reason}") from exc
            except (TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                last_error = exc
        raise ExtractionError(f"no usable answer from model: {type(last_error).__name__}: {last_error}")


def _clean(raw: dict) -> dict:
    """Coerce model output to the schema shape, dropping malformed entries."""

    def s(value) -> str:
        return " ".join(value.split()) if isinstance(value, str) else ""

    if not isinstance(raw, dict):
        raw = {}
    people = []
    for p in raw.get("people") or []:
        if isinstance(p, dict) and s(p.get("full_name")):
            aliases = [s(a) for a in p.get("aliases") or [] if s(a)]
            people.append({"full_name": s(p["full_name"]), "aliases": aliases, "role": s(p.get("role"))})
    events = []
    for e in raw.get("events") or []:
        if isinstance(e, dict) and s(e.get("summary")):
            actors = [s(a) for a in e.get("actors") or [] if s(a)]
            kind = s(e.get("kind")).lower()
            importance = s(e.get("importance")).lower()
            events.append(
                {
                    "date": s(e.get("date")),
                    "kind": kind if kind in KINDS else "other",
                    "importance": importance if importance in IMPORTANCE else "standard",
                    "actors": actors,
                    "summary": s(e["summary"]),
                    "quote": s(e.get("quote")),
                }
            )
    return {"people": people, "events": events}
