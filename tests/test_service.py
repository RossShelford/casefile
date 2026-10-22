import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from casefile.extract import ModelConfig
from casefile.service import Settings, create_app

H = {"X-Casefile": "1"}


class _FakeModel(BaseHTTPRequestHandler):
    """Answers like Ollama: a major event for a notice, a minor one for an acknowledgement."""

    def log_message(self, *args):
        pass

    def _send(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"models": [{"name": "fake:latest"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/pull":  # Ollama streams one JSON line per update, per layer
            lines = [{"status": "pulling manifest"},
                     {"status": "pulling a", "digest": "a", "total": 300, "completed": 100},
                     {"status": "pulling b", "digest": "b", "total": 700, "completed": 700},
                     {"status": "pulling a", "digest": "a", "total": 300, "completed": 300},
                     {"status": "success"}]
            data = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        text = body["messages"][1]["content"]
        events = []
        if "served notice" in text:
            events.append({"date": "2023-04-26", "kind": "legal", "importance": "major", "actors": ["Robert Hale"],
                           "summary": "Robert Hale served notice to end the tenancy.",
                           "quote": "Robert Hale served notice to end the tenancy"})
        if "Thanks for" in text:
            events.append({"date": "2023-01-20", "kind": "message", "importance": "minor", "actors": ["Robert Hale"],
                           "summary": "Robert Hale acknowledged the email.", "quote": "Thanks for your email, Robert Hale"})
        people = [{"full_name": "Robert Hale", "aliases": [], "role": "landlord"}] if events else []
        self._send({"message": {"content": json.dumps({"people": people, "events": events})}})


@pytest.fixture
def model_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _settings(tmp_path, url, token=None):
    return Settings(
        model=ModelConfig(model="fake", base_url=url, api="ollama", num_ctx=8192, timeout=10),
        cache_dir=tmp_path / "parts",
        jobs_dir=tmp_path / "jobs",
        desktop_token=token,
    )


def _client(settings):
    return TestClient(create_app(settings), base_url="http://127.0.0.1")


def _wait(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _upload(client, job_id, files):
    form = [("paths", (None, path)) for path in files] + [("files", ("upload", body)) for body in files.values()]
    return client.post(f"/api/jobs/{job_id}/files", files=form, headers=H)


def test_folder_becomes_a_prioritised_report_and_uploads_are_removed(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    client = _client(settings)
    assert client.get("/api/health").json()["ok"] is True

    job = client.post("/api/jobs", json={"folder": "Harbour Road", "not_sent": ["photos/kitchen.jpg"]}, headers=H).json()
    assert _upload(client, job["id"], {
        "letters/notice.txt": b"On 26 April Robert Hale served notice to end the tenancy.",
        "emails/reply.txt": b"Thanks for your email, Robert Hale.",
    }).status_code == 200
    client.post(f"/api/jobs/{job['id']}/start", headers=H)
    assert _wait(client, job["id"])["status"] == "done"

    report = client.get(f"/api/jobs/{job['id']}/report").json()
    by_importance = {e["importance"]: e for e in report["events"]}
    assert by_importance["major"]["icon"] == "scale"
    assert by_importance["major"]["summary"] == "Robert served notice to end the tenancy."
    assert by_importance["minor"]["source"] == "emails/reply.txt, line 1"
    assert report["counts"] == {"minor": 1, "standard": 0, "notable": 0, "major": 1}
    assert {s["path"]: s["status"] for s in report["sources"]}["photos/kitchen.jpg"].startswith("skipped")

    pdf = client.get(f"/api/jobs/{job['id']}/report.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert not (settings.jobs_dir / job["id"] / "upload").exists()


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "a/../../b.txt", "C:/x.txt"])
def test_paths_that_could_escape_the_upload_folder_are_refused(tmp_path, model_url, bad):
    settings = _settings(tmp_path, model_url)
    client = _client(settings)
    job = client.post("/api/jobs", json={"folder": "f"}, headers=H).json()
    assert _upload(client, job["id"], {"ok.txt": b"fine", bad: b"evil"}).status_code == 400
    written = [p for p in (settings.jobs_dir / job["id"]).rglob("*") if p.is_file() and p.name != "job.json"]
    assert written == []  # the whole batch is refused, including the safe file
    assert not (tmp_path / "escape.txt").exists()


def test_other_websites_cannot_create_jobs_or_rebind_dns(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    assert _client(settings).post("/api/jobs", json={"folder": "f"}).status_code == 403
    evil = TestClient(create_app(settings), base_url="http://attacker.example")
    assert evil.get("/api/jobs").status_code == 400


def test_a_job_interrupted_by_a_restart_runs_again(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    client = _client(settings)
    job = client.post("/api/jobs", json={"folder": "f"}, headers=H).json()
    _upload(client, job["id"], {"notice.txt": b"Robert Hale served notice to end the tenancy."})
    meta = settings.jobs_dir / job["id"] / "job.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "status": "running"}))  # service died mid-run

    restarted = _client(settings)
    assert _wait(restarted, job["id"])["status"] == "done"
    assert restarted.get(f"/api/jobs/{job['id']}/report").json()["counts"]["major"] == 1


def test_model_server_down_fails_the_job_with_directions(tmp_path):
    settings = _settings(tmp_path, "http://127.0.0.1:9")
    client = _client(settings)
    health = client.get("/api/health").json()
    assert health["ok"] is False and "ollama serve" in health["message"]
    job = client.post("/api/jobs", json={"folder": "f"}, headers=H).json()
    _upload(client, job["id"], {"a.txt": b"text"})
    client.post(f"/api/jobs/{job['id']}/start", headers=H)
    failed = _wait(client, job["id"])
    assert failed["status"] == "failed" and "Start the model server" in failed["error"]


TOKEN = "t" * 64


def test_desktop_engine_answers_only_the_app(tmp_path, model_url):
    settings = _settings(tmp_path, model_url, TOKEN)
    stranger = _client(settings)
    assert stranger.get("/").status_code == 403
    assert stranger.get("/api/jobs").status_code == 403
    app = _client(settings)
    app.cookies.set("casefile_token", TOKEN)
    assert app.get("/api/config").json()["desktop"] is True


def test_desktop_reads_the_folder_in_place_and_never_changes_it(tmp_path, model_url):
    folder = tmp_path / "Harbour Road"
    (folder / "letters").mkdir(parents=True)
    (folder / "letters" / "notice.txt").write_text("Robert Hale served notice to end the tenancy.")
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    settings = _settings(tmp_path, model_url, TOKEN)
    app = _client(settings)
    app.cookies.set("casefile_token", TOKEN)
    job = app.post("/api/jobs/local", json={"path": str(folder)}, headers=H).json()
    assert job["title"] == "Harbour Road"
    assert _wait(app, job["id"])["status"] == "done"
    report = app.get(f"/api/jobs/{job['id']}/report").json()
    assert report["counts"]["major"] == 1 and report["folder"] == "Harbour Road"

    assert app.delete(f"/api/jobs/{job['id']}", headers=H).status_code == 204
    assert not (settings.jobs_dir / job["id"]).exists()
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


def test_reading_a_folder_by_path_is_refused_outside_the_app(tmp_path, model_url):
    client = _client(_settings(tmp_path, model_url))
    assert client.post("/api/jobs/local", json={"path": str(tmp_path)}, headers=H).status_code == 403


def test_first_run_can_download_the_model_with_progress(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    settings.model.model = "missing"
    client = _client(settings)
    assert client.get("/api/health").json()["problem"] == "model"
    client.post("/api/model/pull", headers=H)
    deadline = time.time() + 5
    while (state := client.get("/api/model/pull").json())["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert state == {"status": "done", "completed": 1000, "total": 1000, "error": ""}
