"""Local web service: choose a folder, get a prioritised event log back.

Two ways in:
- Browser (`casefile serve`): a browser can't hand a server a folder path, so the page sends
  the files in batches. Uploaded copies are deleted as soon as the job ends (done, failed or
  cancelled).
    POST /api/jobs                  {title, folder, not_sent}   -> job (status "uploading")
    POST /api/jobs/{id}/files       multipart batches of files + their relative paths
    POST /api/jobs/{id}/start       -> queued; one worker runs jobs in order
- Desktop app (`casefile serve --desktop`): the app's own folder dialog gives a real path,
  and the folder is read where it is. Nothing is copied, and the folder is never modified.
    POST /api/jobs/local            {path, title}               -> queued

Then:
  GET  /api/jobs/{id}             status (uploading, queued, running, done, failed, cancelled)
                                  and progress
  POST /api/jobs/{id}/cancel      stop: at once if waiting, after the part in flight if running
  GET  /api/jobs/{id}/report      the log as JSON;  /report.pdf  the PDF
  DELETE /api/jobs/{id}           remove the job, its report, any uploaded copies and the cached
                                  model results only it used (never the documents)
  DELETE /api/data                remove every job and the whole cache of model results
  GET  /api/health                is the model ready; POST/GET /api/model/pull downloads it

Every request needs the casefile_token cookie. Browser mode: opening the printed link with
?token=... sets it.
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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .export import report_to_dict
from .extract import Extractor, ModelConfig, ModelServerError, check_model, pull_model
from .icons import ASSETS
from .ingest import SUPPORTED_EXTS
from .pipeline import Cancelled, FolderNotFound, build_report
from .render import render
from .taxonomy import IMPORTANCE_COLOR, IMPORTANCE_LABEL, IMPORTANCE_MEANING, KIND_ICON

log = logging.getLogger("casefile")
WEB = Path(__file__).parent / "web"
# Every state-changing API call must carry this header. Browsers won't let another website add
# it without a CORS preflight, which this service never approves, so other sites can't create
# or delete jobs. Reads need no header: other sites can't read the responses anyway, and the
# PDF link must work as a plain download.
CLIENT_HEADER = "x-casefile"
# A random secret made at each start (by `casefile serve`, or by the desktop app, which sets it
# as a cookie in its own window). Every request without it is refused, so no other program or
# user on the computer can use the service, even though it listens on a local port.
TOKEN_COOKIE = "casefile_token"


def _refuse(request: Request, message: str) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=403)
    return PlainTextResponse(message, status_code=403)  # the page itself: shown as-is


@dataclass
class Settings:
    model: ModelConfig
    cache_dir: Path
    jobs_dir: Path
    token: str  # every request must carry it as the casefile_token cookie
    chunk_size: int = 6000
    overlap: int = 600
    parallel: int = 1
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    desktop: bool = False  # run by the casefile app: enables local-folder jobs


# uploading -> queued -> running -> done | failed | cancelled
FINAL = ("done", "failed", "cancelled")


@dataclass
class Job:
    id: str
    title: str
    folder: str
    status: str = "uploading"
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


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


class JobManager:
    """Jobs, their files on disk, and the one worker thread that runs them.

    Privacy rules: uploaded copies live only while a job waits or runs; they are deleted when it
    ends, however it ends. Deleting a job also deletes the cached model results for its parts
    (names, summaries and quotes from the documents) unless another kept job uses them.

    self.lock guards job status changes and the job list, so the worker and the request
    threads never act on a half-changed job.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self.cache_keys: dict[str, set[str]] = {}  # job id -> parts cache keys its last run used
        self.stopping: dict[str, str] = {}  # running job id -> "cancel" or "delete"
        self.active: Extractor | None = None  # the running job's extractor
        self.cache = Extractor(settings.model, settings.cache_dir)  # only for cache_path()
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

    def _save_keys(self, job_id: str, keys: set[str]) -> None:
        (self.dir(job_id) / "parts.json").write_text(json.dumps(sorted(keys)))

    def _load_keys(self, job_id: str) -> set[str]:
        try:
            return {k for k in json.loads((self.dir(job_id) / "parts.json").read_text()) if isinstance(k, str)}
        except (OSError, ValueError, TypeError):
            return set()

    def _drop_upload(self, job: Job) -> None:
        """Delete the uploaded copies. (A desktop job reads the user's own folder in place;
        that is never touched.)"""
        if not job.source_path:
            shutil.rmtree(self.dir(job.id) / "upload", ignore_errors=True)

    def _keys_in_use(self) -> set[str]:
        """Cache keys still needed by kept jobs. Call with self.lock held."""
        keys: set[str] = set()
        for job_id in self.jobs:
            keys |= self.cache_keys.get(job_id, set())
        if self.active is not None:
            keys |= set(self.active.keys_used)
        return keys

    def _remove(self, job_id: str, keys: set[str]) -> None:
        """Delete a job's folder and the cached parts no other job uses."""
        for key in keys:
            try:
                self.cache.cache_path(key).unlink(missing_ok=True)
            except OSError:
                log.exception("could not delete cached part %s", key)
        shutil.rmtree(self.dir(job_id), ignore_errors=True)

    def _restore(self) -> None:
        """Pick up where a restarted service left off: queued or interrupted jobs run again,
        in the order they were created (finished parts are cached, so little work is repeated)."""
        found: list[Job] = []
        for meta in self.settings.jobs_dir.glob("*/job.json"):
            try:
                found.append(Job(**json.loads(meta.read_text())))
            except (OSError, ValueError, TypeError):
                log.warning("skipping unreadable job file %s", meta)
        for job in sorted(found, key=lambda j: j.created):
            if job.status in ("queued", "running"):
                job.status, job.done, job.current = "queued", 0, ""
                self.queue.put(job.id)
            elif job.status == "uploading":
                job.status, job.error = "failed", "The upload was interrupted. Choose the folder again."
                job.finished = time.time()
            if job.status in FINAL:
                self._drop_upload(job)  # also clears copies left by older versions
            self.cache_keys[job.id] = self._load_keys(job.id)
            self.jobs[job.id] = job
            try:
                self._save(job)
            except OSError:
                log.exception("could not save job %s", job.id)

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
            raise HTTPException(409, f"This job is {job.status}; files can't be added now.")
        target = self.upload_root(job).joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            shutil.copyfileobj(upload.file, out, 1024 * 1024)
        with self.lock:
            # The job may have been cancelled or deleted while this file was being written;
            # don't leave the copy behind.
            if self.jobs.get(job.id) is not job:
                shutil.rmtree(self.dir(job.id), ignore_errors=True)
                raise HTTPException(404, "No such job. It may have been deleted.")
            if job.status != "uploading":
                self._drop_upload(job)
                raise HTTPException(409, f"This job is {job.status}; files can't be added now.")
            job.files += 1

    def start(self, job: Job) -> None:
        with self.lock:
            if job.status != "uploading":
                raise HTTPException(409, f"This job is already {job.status}.")
            job.status = "queued"
            self._save(job)
        self.queue.put(job.id)

    def cancel(self, job: Job) -> None:
        """A waiting job stops at once; a running one when the part in flight is finished."""
        with self.lock:
            if job.status == "running":
                self.stopping.setdefault(job.id, "cancel")
                return
            if job.status not in ("uploading", "queued"):
                return  # already finished
            job.status, job.finished, job.current = "cancelled", time.time(), ""
            self._save(job)
            self._drop_upload(job)

    def delete(self, job_id: str) -> None:
        """Remove a job, its report, its uploaded copies and its unshared cached parts. A running
        job disappears from the list at once; the worker removes its files when it stops."""
        with self.lock:
            job = self.jobs.pop(job_id, None)
            if job is None:
                return
            if job.status == "running":
                self.stopping[job_id] = "delete"
                return
            keys = self.cache_keys.pop(job_id, set()) - self._keys_in_use()
        self._remove(job_id, keys)

    def delete_all(self) -> None:
        """Every job and the whole parts cache. A running job is stopped and removed by the worker."""
        with self.lock:
            running = {j.id for j in self.jobs.values() if j.status == "running"}
            for job_id in running:
                self.stopping[job_id] = "delete"
            self.jobs.clear()
            self.cache_keys = {k: v for k, v in self.cache_keys.items() if k in running}
            # Under the lock, so a job created meanwhile isn't half-deleted.
            for child in self.settings.jobs_dir.iterdir():
                if child.name not in running:
                    _remove_path(child)
            # Keep the folder itself: the running job's extractor may still write its last part,
            # which the worker then deletes.
            for child in self.settings.cache_dir.iterdir():
                _remove_path(child)

    # -- worker ------------------------------------------------------------------

    def _work(self) -> None:
        # One failing job (or a full disk) must never kill the worker: every later job would
        # sit "queued" forever.
        while True:
            job_id = self.queue.get()
            try:
                with self.lock:
                    job = self.jobs.get(job_id)
                    if job is None or job.status != "queued":
                        continue  # deleted or cancelled while waiting
                    job.status, job.started, job.finished, job.error = "running", time.time(), None, ""
                    job.done, job.total, job.current, job.counts = 0, 0, "", {}
                    extractor = Extractor(self.settings.model, self.settings.cache_dir)
                    self.active = extractor
                    self._save(job)
                self._run(job, extractor)
            except Exception:
                log.exception("worker: job %s", job_id)

    def _run(self, job: Job, extractor: Extractor) -> None:
        s = self.settings
        status, error, counts = "failed", "", {}

        def progress(done: int, total: int, label: str) -> None:
            job.done, job.total, job.current = done, total, label

        try:
            report = build_report(
                self.upload_root(job),
                extractor,
                title=job.title,
                chunk_size=s.chunk_size,
                overlap=s.overlap,
                jobs=s.parallel,
                progress=progress,
                not_sent=job.not_sent,
                should_stop=lambda: job.id in self.stopping,
            )
            data = report_to_dict(report)
            (self.dir(job.id) / "report.json").write_text(json.dumps(data))
            render(report, str(self.dir(job.id) / "report.pdf"))
            status, counts = "done", data["counts"]
        except Cancelled:
            status = "cancelled"
        except FolderNotFound as exc:
            error = str(exc)
        except ModelServerError as exc:
            error = f"{exc}. Start the model server, then choose the folder again."
        except Exception as exc:  # report the failure to the user instead of a silent dead job
            log.exception("job %s failed", job.id)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finish(job, extractor, status, error, counts)

    def _finish(self, job: Job, extractor: Extractor, status: str, error: str, counts: dict) -> None:
        # Privacy: uploaded copies are deleted as soon as the job ends, however it ended, and
        # before the new status is shown.
        self._drop_upload(job)
        with self.lock:
            self.active = None
            keys = set(extractor.keys_used)
            if self.stopping.pop(job.id, None) == "delete":
                keys = (keys | self.cache_keys.pop(job.id, set())) - self._keys_in_use()
                removed = True
            else:
                removed = False
                job.status, job.error, job.current, job.finished = status, error, "", time.time()
                if status == "done":
                    job.counts = counts
                self.cache_keys[job.id] = keys
                try:
                    self._save_keys(job.id, keys)
                    self._save(job)
                except OSError:
                    log.exception("could not save job %s", job.id)
        if removed:
            self._remove(job.id, keys)


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
    desktop = settings.desktop
    expected = settings.token.encode()
    app = FastAPI(title="casefile", docs_url=None, redoc_url=None)
    app.state.manager = manager
    refusal = (
        "This casefile engine only answers the casefile app." if desktop
        else "Open casefile from the link printed when it started."
    )

    @app.middleware("http")
    async def require_client(request: Request, call_next):
        given = request.query_params.get("token")
        if given is not None and not desktop:
            # The link printed by `casefile serve`: swap the key in the address for a cookie
            # and drop it from the address bar, so it isn't kept in history or bookmarks.
            if not hmac.compare_digest(given.encode(), expected):
                return _refuse(request, refusal)
            rest = request.url.remove_query_params("token").query
            response = RedirectResponse(request.url.path + (f"?{rest}" if rest else ""), status_code=303)
            response.set_cookie(TOKEN_COOKIE, settings.token, httponly=True, samesite="strict", path="/")
            return response
        if not hmac.compare_digest(request.cookies.get(TOKEN_COOKIE, "").encode(), expected):
            return _refuse(request, refusal)
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and request.url.path.startswith("/api/") and request.headers.get(CLIENT_HEADER) != "1":
            return JSONResponse({"detail": "Missing X-Casefile header."}, status_code=403)
        return await call_next(request)

    # Added last, so it runs first: a request under a name other than this computer's
    # (DNS rebinding) is refused before anything else looks at it.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

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

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        job = manager.get(job_id)
        manager.cancel(job)
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
        # Always 204, even for an unknown or running job: either way it is gone (or going).
        manager.delete(job_id)

    @app.delete("/api/data", status_code=204)
    def delete_data():
        manager.delete_all()

    return app
