"""Local web service: choose a folder, get a prioritised event log back.

Two ways in:
- Browser (`casefile serve`): a browser can't hand a server a folder path, so the page sends
  the files in batches. Uploaded copies are deleted as soon as the report is built.
    POST /api/jobs                  {title, folder, not_sent}   -> job (status "uploading")
    POST /api/jobs/{id}/files       multipart batches of files + their relative paths
    POST /api/jobs/{id}/start       -> queued; one worker runs jobs in order
- Desktop app (`casefile serve --desktop`): the app's own folder dialog gives a real path,
  and the folder is read where it is. Nothing is copied, and the folder is never modified.
    POST /api/jobs/local            {path, title}               -> queued

Then:
  GET  /api/jobs/{id}             status and progress
  GET  /api/jobs/{id}/report      the log as JSON;  /report.pdf  the PDF
  DELETE /api/jobs/{id}           remove the job and its report (never the documents)
  GET  /api/health                is the model ready; POST/GET /api/model/pull downloads it
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .export import report_to_dict
from .extract import Extractor, ModelConfig, ModelServerError, check_model, pull_model
from .icons import ASSETS
from .ingest import SUPPORTED_EXTS
from .pipeline import build_report
from .render import render
from .taxonomy import IMPORTANCE_COLOR, IMPORTANCE_LABEL, IMPORTANCE_MEANING, KIND_ICON

log = logging.getLogger("casefile")
WEB = Path(__file__).parent / "web"
# Every state-changing API call must carry this header. Browsers won't let another website add
# it without a CORS preflight, which this service never approves, so other sites can't create
# or delete jobs. Reads need no header: other sites can't read the responses anyway, and the
# PDF link must work as a plain download.
CLIENT_HEADER = "x-casefile"
# Desktop mode: the app creates a random secret at each launch and sets it as a cookie in its
# own window. Every request without it is refused, so no other program or website on the
# computer can use the engine, even though it listens on a local port.
TOKEN_COOKIE = "casefile_token"


@dataclass
class Settings:
    model: ModelConfig
    cache_dir: Path
    jobs_dir: Path
    chunk_size: int = 6000
    overlap: int = 600
    parallel: int = 1
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    desktop_token: str | None = None  # set by the desktop app; enables local-folder jobs


@dataclass
class Job:
    id: str
    title: str
    folder: str
    status: str = "uploading"  # uploading -> queued -> running -> done | failed
    created: float = field(default_factory=time.time)
    files: int = 0
    not_sent: list[str] = field(default_factory=list)
    done: int = 0  # parts processed
    total: int = 0
    current: str = ""
    started: float | None = None
    finished: float | None = None
    error: str = ""
    counts: dict = field(default_factory=dict)
    source_path: str = ""  # desktop: the user's folder, read in place and never modified

    def public(self) -> dict:
        data = asdict(self)
        data.pop("not_sent")
        data["not_sent"] = len(self.not_sent)
        if self.status == "running" and self.done and self.started:
            elapsed = time.time() - self.started
            data["eta_seconds"] = round(elapsed / self.done * (self.total - self.done))
        return data


def safe_relpath(raw: str) -> PurePosixPath:
    """A browser-supplied relative path, refused if it could escape the upload folder."""
    path = PurePosixPath(raw.replace("\\", "/"))
    parts = [p for p in path.parts if p not in ("", ".")]
    if path.is_absolute() or not parts or any(p == ".." or ":" in p for p in parts):
        raise HTTPException(400, f"Unsafe file path: {raw!r}")
    return PurePosixPath(*parts)


def safe_name(raw: str) -> str:
    name = re.sub(r"[^\w .()-]+", "_", raw).strip(" .") or "folder"
    return name[:80]


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._restore()
        self.worker = threading.Thread(target=self._work, name="casefile-worker", daemon=True)
        self.worker.start()

    # -- storage -----------------------------------------------------------------

    def dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def upload_root(self, job: Job) -> Path:
        if job.source_path:
            return Path(job.source_path)
        return self.dir(job.id) / "upload" / job.folder

    def _save(self, job: Job) -> None:
        tmp = self.dir(job.id) / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(job)))
        tmp.replace(self.dir(job.id) / "job.json")

    def _restore(self) -> None:
        """Pick up where a restarted service left off: queued or interrupted jobs run again
        (finished parts are cached, so little work is repeated)."""
        for meta in sorted(self.settings.jobs_dir.glob("*/job.json")):
            try:
                job = Job(**json.loads(meta.read_text()))
            except (ValueError, TypeError):
                continue
            if job.status in ("queued", "running"):
                job.status, job.done, job.current = "queued", 0, ""
                self.queue.put(job.id)
            elif job.status == "uploading":
                job.status, job.error = "failed", "The upload was interrupted. Choose the folder again."
            self.jobs[job.id] = job
            self._save(job)

    # -- lifecycle ---------------------------------------------------------------

    def create(self, title: str, folder: str, not_sent: list[str]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], title=title.strip() or folder, folder=safe_name(folder))
        job.not_sent = [str(safe_relpath(p)) for p in not_sent]
        self.upload_root(job).mkdir(parents=True)
        with self.lock:
            self.jobs[job.id] = job
        self._save(job)
        return job

    def create_local(self, path: str, title: str) -> Job:
        folder = Path(path).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise HTTPException(400, f"Not a folder: {path}")
        job = Job(id=uuid.uuid4().hex[:12], title=title.strip() or folder.name, folder=safe_name(folder.name),
                  status="queued", source_path=str(folder.resolve()))
        self.dir(job.id).mkdir(parents=True)
        with self.lock:
            self.jobs[job.id] = job
        self._save(job)
        self.queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job. It may have been deleted.")
        return job

    def add_file(self, job: Job, rel: PurePosixPath, upload: UploadFile) -> None:
        if job.status != "uploading":
            raise HTTPException(409, "This job has already started; files can't be added now.")
        target = self.upload_root(job).joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            shutil.copyfileobj(upload.file, out, 1024 * 1024)
        with self.lock:
            job.files += 1

    def start(self, job: Job) -> None:
        if job.status != "uploading":
            raise HTTPException(409, f"This job is already {job.status}.")
        job.status = "queued"
        self._save(job)
        self.queue.put(job.id)

    def delete(self, job: Job) -> None:
        if job.status == "running":
            raise HTTPException(409, "This job is running. Wait for it to finish, then delete it.")
        with self.lock:
            self.jobs.pop(job.id, None)
        shutil.rmtree(self.dir(job.id), ignore_errors=True)

    # -- worker ------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued":
                continue  # deleted while waiting
            self._run(job)

    def _run(self, job: Job) -> None:
        s = self.settings
        job.status, job.started, job.error = "running", time.time(), ""
        self._save(job)

        def progress(done: int, total: int, label: str) -> None:
            job.done, job.total, job.current = done, total, label

        try:
            report = build_report(
                self.upload_root(job),
                Extractor(s.model, s.cache_dir),
                title=job.title,
                chunk_size=s.chunk_size,
                overlap=s.overlap,
                jobs=s.parallel,
                progress=progress,
                not_sent=job.not_sent,
            )
            data = report_to_dict(report)
            (self.dir(job.id) / "report.json").write_text(json.dumps(data))
            render(report, str(self.dir(job.id) / "report.pdf"))
            job.counts = data["counts"]
            job.status = "done"
            if not job.source_path:
                # Privacy: the uploaded copies are no longer needed once the report exists.
                # (A desktop job read the user's own folder in place; that is never touched.)
                shutil.rmtree(self.dir(job.id) / "upload", ignore_errors=True)
        except ModelServerError as exc:
            job.status, job.error = "failed", f"{exc}. Start the model server, then choose the folder again."
        except Exception as exc:  # report the failure to the user instead of a silent dead job
            log.exception("job %s failed", job.id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        job.finished = time.time()
        job.current = ""
        self._save(job)


class ModelPull:
    """Downloads the model in the background so the first-run screen can show progress."""

    def __init__(self, config: ModelConfig):
        self.config = config
        self.state = {"status": "idle", "completed": 0, "total": 0, "error": ""}
        self.lock = threading.Lock()

    def start(self) -> dict:
        with self.lock:
            if self.state["status"] != "running":
                self.state = {"status": "running", "completed": 0, "total": 0, "error": ""}
                threading.Thread(target=self._run, name="casefile-model-pull", daemon=True).start()
            return dict(self.state)

    def _run(self) -> None:
        def progress(completed: int, total: int) -> None:
            self.state.update(completed=completed, total=total)

        try:
            pull_model(self.config, progress)
            self.state["status"] = "done"
        except (ModelServerError, OSError, ValueError) as exc:
            self.state.update(status="failed", error=str(exc))


class NewJob(BaseModel):
    title: str = ""
    folder: str
    not_sent: list[str] = []


class LocalJob(BaseModel):
    path: str
    title: str = ""


def create_app(settings: Settings) -> FastAPI:
    manager = JobManager(settings)
    puller = ModelPull(settings.model)
    desktop = settings.desktop_token is not None
    app = FastAPI(title="casefile", docs_url=None, redoc_url=None)
    app.state.manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def require_client(request: Request, call_next):
        if desktop:
            token = request.cookies.get(TOKEN_COOKIE, "")
            if not hmac.compare_digest(token.encode(), settings.desktop_token.encode()):
                return JSONResponse({"detail": "This casefile engine only answers the casefile app."}, status_code=403)
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and request.url.path.startswith("/api/") and request.headers.get(CLIENT_HEADER) != "1":
            return JSONResponse({"detail": "Missing X-Casefile header."}, status_code=403)
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/api/config")
    def config():
        return {
            "model": settings.model.model,
            "desktop": desktop,
            "can_download_model": settings.model.api == "ollama",
            "extensions": sorted(SUPPORTED_EXTS),
            "importance": {
                k: {"label": IMPORTANCE_LABEL[k], "color": v, "meaning": IMPORTANCE_MEANING[k]}
                for k, v in IMPORTANCE_COLOR.items()
            },
            "kinds": KIND_ICON,
        }

    @app.get("/api/health")
    def health():
        problem, message = check_model(settings.model)
        return {"ok": problem is None, "problem": problem, "message": message}

    @app.get("/api/model/pull")
    def model_pull_status():
        return puller.state

    @app.post("/api/model/pull")
    def model_pull():
        if settings.model.api != "ollama":
            raise HTTPException(400, "Only Ollama can download models. Load the model in your model server instead.")
        return puller.start()

    @app.post("/api/jobs/local", status_code=201)
    def create_local_job(body: LocalJob):
        if not desktop:
            raise HTTPException(403, "Reading a folder by path is only available in the casefile app.")
        return manager.create_local(body.path, body.title).public()

    @app.get("/api/jobs")
    def list_jobs():
        jobs = sorted(manager.jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.public() for j in jobs]

    @app.post("/api/jobs", status_code=201)
    def create_job(body: NewJob):
        return manager.create(body.title, body.folder, body.not_sent).public()

    @app.post("/api/jobs/{job_id}/files")
    def upload(job_id: str, files: list[UploadFile] = File(...), paths: list[str] = Form(...)):
        job = manager.get(job_id)
        if len(files) != len(paths):
            raise HTTPException(400, "Each file needs exactly one path.")
        rels = [safe_relpath(p) for p in paths]  # validate the whole batch before writing any of it
        for rel, upload_file in zip(rels, files):
            manager.add_file(job, rel, upload_file)
        return {"received": job.files}

    @app.post("/api/jobs/{job_id}/start")
    def start(job_id: str):
        job = manager.get(job_id)
        manager.start(job)
        return job.public()

    @app.get("/api/jobs/{job_id}")
    def status(job_id: str):
        return manager.get(job_id).public()

    def _finished(job_id: str, name: str) -> Path:
        job = manager.get(job_id)
        path = manager.dir(job.id) / name
        if job.status != "done" or not path.exists():
            raise HTTPException(409, f"The report isn't ready: the job is {job.status}.")
        return path

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str):
        return json.loads(_finished(job_id, "report.json").read_text())

    @app.get("/api/jobs/{job_id}/report.pdf")
    def report_pdf(job_id: str):
        job = manager.get(job_id)
        filename = f"{safe_name(job.title)}.pdf"
        return FileResponse(_finished(job_id, "report.pdf"), media_type="application/pdf", filename=filename)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete(job_id: str):
        manager.delete(manager.get(job_id))

    return app
