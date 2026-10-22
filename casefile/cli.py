from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from .extract import Extractor, ModelConfig, ModelServerError

DATA = Path.home() / ".cache" / "casefile"


def _model_options() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("model")
    g.add_argument("--model", default="qwen2.5:7b", help="model name on the local server (default: %(default)s)")
    g.add_argument(
        "--api", choices=["ollama", "openai"], default="ollama",
        help="'ollama' (default), or 'openai' for any OpenAI-compatible local server: LM Studio, llama.cpp, vLLM",
    )
    g.add_argument("--url", default=None, help="model server address (default: http://localhost:11434, or :1234 for --api openai)")
    g.add_argument("--context", type=int, default=8192, help="context window to request from Ollama (default: %(default)s)")
    g.add_argument("--chunk-size", type=int, default=6000, help="characters per model request (default: %(default)s)")
    g.add_argument("--overlap", type=int, default=600, help="characters shared by neighbouring parts (default: %(default)s)")
    g.add_argument("--jobs", type=int, default=1, help="parallel model requests; raise only if the server allows it")
    g.add_argument("--timeout", type=float, default=600, help="seconds to wait for one model reply (default: %(default)s)")
    g.add_argument("--cache", type=Path, default=DATA / "parts",
                   help="where per-part results are kept so reruns resume (default: %(default)s)")
    return p


def _config(args: argparse.Namespace) -> ModelConfig:
    url = args.url or ("http://localhost:11434" if args.api == "ollama" else "http://localhost:1234")
    return ModelConfig(model=args.model, base_url=url, api=args.api, num_ctx=args.context, timeout=args.timeout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="casefile",
        description="Turn a folder of documents into a prioritised log of events, using a local AI model.",
    )
    sub = ap.add_subparsers(dest="command", required=True)
    shared = _model_options()

    rp = sub.add_parser("report", parents=[shared], help="read a folder and write a PDF")
    rp.add_argument("folder", type=Path, help="folder to read (searched recursively)")
    rp.add_argument("-o", "--output", type=Path, help="PDF to write (default: <folder>.pdf next to the folder)")
    rp.add_argument("--title", help="report title (default: folder name)")
    rp.add_argument("--no-quotes", action="store_true", help="leave supporting quotes out of the main log")

    sp = sub.add_parser("serve", parents=[shared], help="run the web page: choose a folder in the browser")
    sp.add_argument("--host", default="127.0.0.1", help="address to listen on (default: %(default)s, this computer only)")
    sp.add_argument("--port", type=int, default=8765, help="port (default: %(default)s)")
    sp.add_argument("--data", type=Path, default=DATA / "jobs", help="where reports are kept (default: %(default)s)")
    sp.add_argument("--allow-host", action="append", default=[],
                    help="extra host name the page may be opened under (repeatable), e.g. a LAN name")
    sp.add_argument("--desktop", action="store_true",
                    help="run as the engine of the casefile app: needs CASEFILE_TOKEN in the environment, "
                         "reads folders in place, and exits when the app closes its stdin")

    args = ap.parse_args(argv)
    if args.overlap >= args.chunk_size:
        ap.error("--overlap must be smaller than --chunk-size")
    return _report(ap, args) if args.command == "report" else _serve(args)


def _report(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from .pipeline import build_report
    from .render import render

    folder: Path = args.folder.resolve()
    if not folder.is_dir():
        ap.error(f"not a folder: {folder}")
    output = (args.output or folder.with_suffix(".pdf")).resolve()
    extractor = Extractor(_config(args), args.cache.expanduser().resolve())
    started = time.monotonic()
    interactive = sys.stderr.isatty()

    def progress(done: int, total: int, label: str) -> None:
        if done == 0:
            return
        elapsed = time.monotonic() - started
        eta = elapsed / done * (total - done)
        line = f"[{done}/{total}] {elapsed:,.0f}s elapsed, ~{eta:,.0f}s left  {label}"
        if interactive:
            sys.stderr.write("\r\033[K" + line[:160])
            if done == total:
                sys.stderr.write("\n")
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    try:
        report = build_report(
            folder,
            extractor,
            title=args.title or folder.name,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            jobs=args.jobs,
            progress=progress,
        )
    except ModelServerError as exc:
        print(f"\ncasefile: {exc}", file=sys.stderr)
        if args.api == "ollama":
            print(f"Is Ollama running (`ollama serve`) and is the model pulled (`ollama pull {args.model}`)?", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncasefile: stopped. Finished parts are cached; run again to continue.", file=sys.stderr)
        return 130

    render(report, str(output), quotes=not args.no_quotes)
    counts = {level: sum(1 for e in report.events if e.importance == level) for level in ("major", "notable")}
    print(
        f"{output}\n{len(report.people)} people, {len(report.events)} events "
        f"({counts['major']} major, {counts['notable']} notable), "
        f"{len(report.unverified)} to check, {len(report.failures)} failed parts."
    )
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .service import Settings, create_app

    hosts = ["127.0.0.1", "localhost", *args.allow_host]
    if args.host not in ("127.0.0.1", "localhost", "0.0.0.0", "::"):
        hosts.append(args.host)
    token = None
    if args.desktop:
        token = os.environ.get("CASEFILE_TOKEN", "")
        if len(token) < 32:
            print("casefile: --desktop needs a CASEFILE_TOKEN of at least 32 characters", file=sys.stderr)
            return 2
        if args.host != "127.0.0.1":
            print("casefile: --desktop only listens on 127.0.0.1", file=sys.stderr)
            return 2
        # The app holds our stdin open. When the app quits or crashes, stdin closes, and the
        # engine must not linger in the background holding a port and the model.
        threading.Thread(target=_exit_when_stdin_closes, daemon=True).start()
    elif args.host in ("0.0.0.0", "::"):
        print(
            "casefile: listening on all network interfaces. Anyone who can reach this computer and uses an "
            "allowed host name can upload files and read reports. Add names with --allow-host.",
            file=sys.stderr,
        )
    settings = Settings(
        model=_config(args),
        cache_dir=args.cache.expanduser().resolve(),
        jobs_dir=args.data.expanduser().resolve(),
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        parallel=args.jobs,
        allowed_hosts=hosts,
        desktop_token=token,
    )
    print(f"casefile: open http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0', '::') else args.host}:{args.port}",
          file=sys.stderr, flush=True)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
    return 0


def _exit_when_stdin_closes() -> None:
    try:
        while sys.stdin.buffer.read(1024):
            pass
    finally:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
