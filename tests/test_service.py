import http.cookiejar
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from casefile.cli import main
from casefile.extract import ModelConfig
from casefile.service import Settings, create_app

H = {"X-Casefile": "1"}
TOKEN = "t" * 64


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


class _GatedModel(_FakeModel):
    """The fake model, but each answer waits until the test opens the gate."""

    gate: threading.Event
    entered: threading.Event
    calls: int

    def do_POST(self):
        type(self).calls += 1
        type(self).entered.set()
        type(self).gate.wait(15)
        super().do_POST()


def _start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def model_url():
    server = _start_server(_FakeModel)
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def gated():
    """A model that answers only once released: (url, handler class)."""
    handler = type("Gated", (_GatedModel,), {"gate": threading.Event(), "entered": threading.Event(), "calls": 0})
    server = _start_server(handler)
    yield f"http://127.0.0.1:{server.server_port}", handler
    handler.gate.set()  # never leave a worker blocked
    server.shutdown()


def _settings(tmp_path, url, desktop=False):
    return Settings(
        model=ModelConfig(model="fake", base_url=url, api="ollama", num_ctx=8192, timeout=10),
        cache_dir=tmp_path / "parts",
        jobs_dir=tmp_path / "jobs",
        token=TOKEN,
        desktop=desktop,
    )


def _client(settings, cookie=True):
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    if cookie:
        client.cookies.set("casefile_token", TOKEN)
    return client


def _wait_for(check, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def _wait(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _upload(client, job_id, files):
    form = [("paths", (None, path)) for path in files] + [("files", ("upload", body)) for body in files.values()]
    return client.post(f"/api/jobs/{job_id}/files", files=form, headers=H)


def _job(client, files, folder="f"):
    """A browser job with these files uploaded and started."""
    job = client.post("/api/jobs", json={"folder": folder}, headers=H).json()
    assert _upload(client, job["id"], files).status_code == 200
    client.post(f"/api/jobs/{job['id']}/start", headers=H)
    return job["id"]


def _cached(settings):
    return {p.name for p in settings.cache_dir.iterdir()}


def _parts(settings, job_id):
    return {f"{key}.json" for key in json.loads((settings.jobs_dir / job_id / "parts.json").read_text())}


NOTICE = b"On 26 April Robert Hale served notice to end the tenancy."
REPLY = b"Thanks for your email, Robert Hale."


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
    evil.cookies.set("casefile_token", TOKEN)
    assert evil.get("/api/jobs").status_code == 400


def test_browser_mode_needs_the_key_from_the_printed_link(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    stranger = _client(settings, cookie=False)  # another program or user on this computer
    page = stranger.get("/")
    assert page.status_code == 403 and "link printed" in page.text
    for path in ("/api/jobs", "/api/config", "/static/app.js"):
        assert stranger.get(path).status_code == 403
    assert stranger.post("/api/jobs", json={"folder": "f"}, headers=H).status_code == 403
    assert stranger.get("/?token=wrong", follow_redirects=False).status_code == 403

    user = _client(settings, cookie=False)
    opened = user.get(f"/?token={TOKEN}&view=list", follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "/?view=list"  # key leaves the address bar
    cookie = opened.headers["set-cookie"].lower()
    assert f"casefile_token={TOKEN}" in cookie
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert user.get("/api/jobs").status_code == 200  # the cookie now does the work
    assert user.get("/").status_code == 200


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


def test_model_server_down_fails_the_job_with_directions_and_drops_the_upload(tmp_path):
    settings = _settings(tmp_path, "http://127.0.0.1:9")
    client = _client(settings)
    health = client.get("/api/health").json()
    assert health["ok"] is False and "ollama serve" in health["message"]
    job_id = _job(client, {"a.txt": b"text"})
    failed = _wait(client, job_id)
    assert failed["status"] == "failed" and "Start the model server" in failed["error"]
    assert not (settings.jobs_dir / job_id / "upload").exists()


def test_desktop_engine_answers_only_the_app(tmp_path, model_url):
    settings = _settings(tmp_path, model_url, desktop=True)
    stranger = _client(settings, cookie=False)
    assert stranger.get("/").status_code == 403
    assert stranger.get("/api/jobs").status_code == 403
    # The link trick is for browser mode only: the app sets its cookie itself.
    assert stranger.get(f"/?token={TOKEN}", follow_redirects=False).status_code == 403
    assert _client(settings).get("/api/config").json()["desktop"] is True


def test_desktop_reads_the_folder_in_place_and_never_changes_it(tmp_path, model_url):
    folder = tmp_path / "Harbour Road"
    (folder / "letters").mkdir(parents=True)
    (folder / "letters" / "notice.txt").write_text("Robert Hale served notice to end the tenancy.")
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    settings = _settings(tmp_path, model_url, desktop=True)
    app = _client(settings)
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


class _PartlyBroken(_FakeModel):
    """Fails (HTTP 500, as a crashed model runner would) on one document only."""

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        if b"BROKEN" in raw:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.rfile = io.BytesIO(raw)  # let the normal fake read it again
        super().do_POST()


def test_a_part_the_model_fails_on_is_recorded_and_the_run_still_finishes(tmp_path):
    server = _start_server(_PartlyBroken)
    try:
        settings = _settings(tmp_path, f"http://127.0.0.1:{server.server_port}")
        client = _client(settings)
        job_id = _job(client, {"notice.txt": NOTICE, "odd.txt": b"BROKEN part"})
        assert _wait(client, job_id)["status"] == "done"
        report = client.get(f"/api/jobs/{job_id}/report").json()
        assert report["counts"]["major"] == 1
        assert [f["where"].split(",")[0] for f in report["failures"]] == ["odd.txt"]
    finally:
        server.shutdown()


def test_a_running_job_can_be_cancelled_and_a_waiting_one_stops_at_once(tmp_path, gated):
    url, model = gated
    settings = _settings(tmp_path, url)
    client = _client(settings)
    first = _job(client, {"notice.txt": NOTICE, "reply.txt": REPLY})
    assert model.entered.wait(10)  # the first part is with the model
    second = _job(client, {"other.txt": NOTICE})
    assert client.get(f"/api/jobs/{second}").json()["status"] == "queued"

    stopped = client.post(f"/api/jobs/{second}/cancel", headers=H).json()
    assert stopped["status"] == "cancelled"
    assert not (settings.jobs_dir / second / "upload").exists()

    # A running job finishes the part in flight (so it is cached), then stops.
    assert client.post(f"/api/jobs/{first}/cancel", headers=H).json()["status"] == "running"
    model.gate.set()
    job = _wait(client, first)
    assert job["status"] == "cancelled" and job["done"] == 1
    assert model.calls == 1  # the second part was never sent
    assert not (settings.jobs_dir / first / "upload").exists()
    assert _parts(settings, first) == _cached(settings) and len(_cached(settings)) == 1
    assert client.get(f"/api/jobs/{first}/report").status_code == 409
    assert client.post(f"/api/jobs/{first}/start", headers=H).status_code == 409


def test_deleting_a_running_job_stops_it_and_removes_everything(tmp_path, gated):
    url, model = gated
    settings = _settings(tmp_path, url)
    client = _client(settings)
    job_id = _job(client, {"notice.txt": NOTICE})
    assert model.entered.wait(10)

    assert client.delete(f"/api/jobs/{job_id}", headers=H).status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get("/api/jobs").json() == []
    model.gate.set()
    _wait_for(lambda: not (settings.jobs_dir / job_id).exists())
    assert _cached(settings) == set()  # the part finished after the delete is removed too

    # The worker survived and runs the next job.
    assert _wait(client, _job(client, {"notice.txt": NOTICE}))["status"] == "done"
    assert client.delete("/api/jobs/no-such-job", headers=H).status_code == 204


def test_deleting_a_job_removes_only_the_cached_parts_no_other_job_uses(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    client = _client(settings)
    a = _job(client, {"notice.txt": NOTICE, "reply.txt": REPLY})
    assert _wait(client, a)["status"] == "done"
    b = _job(client, {"notice.txt": NOTICE, "later.txt": b"Robert Hale wrote again."})
    assert _wait(client, b)["status"] == "done"
    assert len(_parts(settings, a) & _parts(settings, b)) == 1  # the same notice.txt
    assert _cached(settings) == _parts(settings, a) | _parts(settings, b)

    assert client.delete(f"/api/jobs/{a}", headers=H).status_code == 204
    assert not (settings.jobs_dir / a).exists()
    assert _cached(settings) == _parts(settings, b)
    assert client.get(f"/api/jobs/{b}/report").json()["counts"]["major"] == 1

    assert client.delete(f"/api/jobs/{b}", headers=H).status_code == 204
    assert _cached(settings) == set()


def test_delete_all_data_removes_every_job_and_the_whole_cache(tmp_path, gated):
    url, model = gated
    model.gate.set()
    settings = _settings(tmp_path, url)
    client = _client(settings)
    done = _job(client, {"notice.txt": NOTICE})
    assert _wait(client, done)["status"] == "done"
    (settings.cache_dir / "from-an-old-version.json").write_text("{}")

    model.gate.clear()
    model.entered.clear()
    _job(client, {"reply.txt": REPLY})  # now running
    assert model.entered.wait(10)
    uploading = client.post("/api/jobs", json={"folder": "g"}, headers=H).json()["id"]
    _upload(client, uploading, {"x.txt": b"x"})

    assert client.delete("/api/data").status_code == 403  # still needs the header
    assert client.delete("/api/data", headers=H).status_code == 204
    assert client.get("/api/jobs").json() == []
    model.gate.set()
    _wait_for(lambda: not any(settings.jobs_dir.iterdir()))
    assert _cached(settings) == set()


def test_startup_removes_interrupted_uploads(tmp_path, model_url):
    settings = _settings(tmp_path, model_url)
    client = _client(settings)
    job = client.post("/api/jobs", json={"folder": "f"}, headers=H).json()
    _upload(client, job["id"], {"notice.txt": NOTICE})  # ...and the service stops before start
    assert (settings.jobs_dir / job["id"] / "upload").exists()

    restarted = _client(settings)
    after = restarted.get(f"/api/jobs/{job['id']}").json()
    assert after["status"] == "failed" and "interrupted" in after["error"]
    assert not (settings.jobs_dir / job["id"] / "upload").exists()


def _queued_job(settings, job_id, created, source):
    (settings.jobs_dir / job_id).mkdir(parents=True)
    meta = {"id": job_id, "title": job_id, "folder": "f", "status": "queued", "created": created,
            "source_path": str(source)}
    (settings.jobs_dir / job_id / "job.json").write_text(json.dumps(meta))


def test_restored_jobs_run_in_the_order_they_were_created(tmp_path, model_url):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "notice.txt").write_bytes(NOTICE)
    settings = _settings(tmp_path, model_url)
    # Ids sort the other way round from creation, so path order would be wrong.
    for job_id, created in (("aaa", 3.0), ("bbb", 1.0), ("ccc", 2.0)):
        _queued_job(settings, job_id, created, folder)

    client = _client(settings)
    jobs = [_wait(client, job_id) for job_id in ("aaa", "bbb", "ccc")]
    assert all(j["status"] == "done" for j in jobs)
    assert [j["id"] for j in sorted(jobs, key=lambda j: j["started"])] == ["bbb", "ccc", "aaa"]


def test_a_job_whose_folder_has_gone_fails_with_a_plain_message(tmp_path, model_url):
    settings = _settings(tmp_path, model_url, desktop=True)
    _queued_job(settings, "gone", 1.0, tmp_path / "unplugged drive" / "case")
    job = _wait(_client(settings), "gone")
    assert job["status"] == "failed"
    assert job["error"].startswith("The folder can't be found:") and "unplugged drive" in job["error"]


def test_serve_on_port_0_prints_the_port_it_listens_on(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "casefile", "serve", "--port", "0", "--url", "http://127.0.0.1:9",
         "--data", str(tmp_path / "jobs"), "--cache", str(tmp_path / "parts")],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    lines: queue.Queue = queue.Queue()

    def read(stream):
        for line in stream:
            lines.put(line)

    try:
        threading.Thread(target=read, args=(proc.stdout,), daemon=True).start()
        threading.Thread(target=read, args=(proc.stderr,), daemon=True).start()
        port = link = None
        deadline = time.time() + 30
        while (port is None or link is None) and time.time() < deadline:
            line = lines.get(timeout=max(0.1, deadline - time.time()))
            if m := re.fullmatch(r"casefile: listening on 127\.0\.0\.1:(\d+)\n", line):
                port = int(m[1])
            if m := re.fullmatch(r"casefile: open (http://127\.0\.0\.1:\d+/\?token=[\w-]{40,})\n", line):
                link = m[1]
        assert port and link and link.startswith(f"http://127.0.0.1:{port}/")

        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=10)
        assert refused.value.code == 403
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert b"<html" in browser.open(link, timeout=10).read().lower()
        assert json.loads(browser.open(f"http://127.0.0.1:{port}/api/config", timeout=10).read())["desktop"] is False
    finally:
        proc.terminate()
        proc.wait(10)


def test_report_is_written_next_to_a_folder_with_a_dot_in_its_name(tmp_path, model_url):
    folder = tmp_path / "Smith v. Jones"
    folder.mkdir()
    (folder / "notice.txt").write_bytes(NOTICE)
    code = main(["report", str(folder), "--url", model_url, "--model", "fake", "--cache", str(tmp_path / "parts")])
    assert code == 0
    assert (tmp_path / "Smith v. Jones.pdf").read_bytes().startswith(b"%PDF")
    assert not (tmp_path / "Smith v.pdf").exists()


def test_clear_cache_deletes_the_cached_parts(tmp_path, capsys):
    cache = tmp_path / "parts"
    cache.mkdir()
    for name in ("a.json", "b.json"):
        (cache / name).write_text("{}")
    assert main(["clear-cache", "--cache", str(cache)]) == 0
    assert list(cache.iterdir()) == []
    assert "removed 2 cached parts" in capsys.readouterr().out
