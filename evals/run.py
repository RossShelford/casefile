"""Run casefile's real pipeline over the labelled cases in evals/cases and score the results.

Needs a local model server, exactly like `casefile report`. From the repository root:

    python -m evals.run --model qwen2.5:7b
    python -m evals.run --model qwen2.5:7b --url http://127.0.0.1:11434 --case tenancy-damp

Each part is sent to the model fresh (a temporary cache) unless --cache is given, so the
numbers reflect the current prompt and model. See evals/score.py for how events are matched.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from casefile.extract import Extractor, ModelConfig, ModelServerError, check_model
from casefile.pipeline import build_report

from .score import Score, format_score, score_case

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
GOLD = HERE / "gold"


def available_cases() -> list[str]:
    return sorted(p.stem for p in GOLD.glob("*.json"))


def report_as_dicts(report) -> tuple[list[dict], list[dict]]:
    events = [{"date": e.date, "summary": e.summary, "quote": e.quote, "importance": e.importance} for e in report.events]
    people = [{"label": p.label, "full_name": p.full_name, "unresolved": p.unresolved} for p in report.people]
    return events, people


def run_case(name: str, extractor: Extractor, args: argparse.Namespace) -> Score:
    gold = json.loads((GOLD / f"{name}.json").read_text(encoding="utf-8"))

    def progress(done: int, total: int, label: str) -> None:
        print(f"\r  {name}: part {done}/{total}", end="", file=sys.stderr, flush=True)

    report = build_report(
        CASES / name, extractor, title=name,
        chunk_size=args.chunk_size, overlap=args.overlap, jobs=args.jobs, progress=progress,
    )
    print(file=sys.stderr)
    # A file the reader couldn't open would sink recall for reasons that have nothing to do
    # with the model: say so next to the numbers.
    for doc in report.documents:
        if doc.status != "ok":
            print(f"  warning: {doc.path}: {doc.status}", file=sys.stderr)
    events, people = report_as_dicts(report)
    return score_case(gold, events, people, unverified=len(report.unverified), failed_parts=len(report.failures))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default="qwen2.5:7b", help="model name on the local server (default: %(default)s)")
    ap.add_argument("--url", default="http://127.0.0.1:11434", help="model server address (default: %(default)s)")
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--context", type=int, default=8192, help="context window to request from Ollama")
    ap.add_argument("--chunk-size", type=int, default=6000)
    ap.add_argument("--overlap", type=int, default=600)
    ap.add_argument("--jobs", type=int, default=1, help="parallel model requests")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--cache", type=Path, help="reuse per-part results from this folder (default: none, every part is sent)")
    ap.add_argument("--case", action="append", choices=available_cases(), help="only this case (repeatable)")
    ap.add_argument("--json", type=Path, help="also write the scores to this file")
    ap.add_argument("--brief", action="store_true", help="numbers only, without missed/extra event lists")
    args = ap.parse_args(argv)

    config = ModelConfig(model=args.model, base_url=args.url, api=args.api, num_ctx=args.context, timeout=args.timeout)
    problem, message = check_model(config)
    if problem:
        print(message, file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="casefile-evals-") as tmp:
        extractor = Extractor(config, (args.cache or Path(tmp)).expanduser().resolve())
        scores: dict[str, Score] = {}
        try:
            for name in args.case or available_cases():
                scores[name] = run_case(name, extractor, args)
        except ModelServerError as exc:
            print(f"\nmodel server error: {exc}", file=sys.stderr)
            return 2

    for name, s in scores.items():
        print(format_score(name, s, details=not args.brief))
        print()
    overall = sum(scores.values(), Score())
    print(format_score(f"overall ({len(scores)} cases, model {args.model})", overall, details=False))
    if args.json:
        result = {"model": args.model, "cases": {n: s.to_dict() for n, s in scores.items()}, "overall": overall.to_dict()}
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
