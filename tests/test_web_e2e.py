"""Browser tests of the web page: the real page and engine, a fake model server, headless Chromium.

Skipped when Playwright or its Chromium isn't installed (`pip install -e .[e2e]` then
`playwright install chromium`).
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from casefile.extract import ModelConfig  # noqa: E402
from casefile.service import Settings, create_app  # noqa: E402

TOKEN = "e2e" + "t" * 61
H = {"X-Casefile": "1"}
FORBIDDEN = "Open casefile from the link printed when it started."

NOTICE = b"On 26 April 2023 Robert Hale served notice to end the tenancy."
INSPECTION = b"On 3 March 2023 Ann Lee inspected the flat and found damp in the bedroom."
REPLY = b"Thanks for your email, Robert Hale. I will reply soon."


class _FakeModel(BaseHTTPRequestHandler):
    """Answers like Ollama. `delay` (seconds per extraction) makes a run slow enough to stop."""

    delay = 0.0

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
        time.sleep(self.delay)
        text = body["messages"][1]["content"]
        events, people = [], []
        if "served notice" in text:
            events.append({"date": "2023-04-26", "kind": "legal", "importance": "major", "actors": ["Robert Hale"],
                           "summary": "Robert Hale served notice to end the tenancy.",
                           "quote": "Robert Hale served notice to end the tenancy"})
            people.append({"full_name": "Robert Hale", "aliases": [], "role": "landlord"})
        if "inspected the flat" in text:
            events.append({"date": "2023-03-03", "kind": "inspection", "importance": "notable", "actors": ["Ann Lee"],
                           "summary": "Ann Lee inspected the flat and found damp.",
                           "quote": "Ann Lee inspected the flat and found damp in the bedroom"})
            people.append({"full_name": "Ann Lee", "aliases": [], "role": "inspector"})
        if "Thanks for" in text:
            events.append({"date": "2023-01-20", "kind": "message", "importance": "minor", "actors": ["Robert Hale"],
                           "summary": "Robert Hale acknowledged the email.",
                           "quote": "Thanks for your email, Robert Hale. I will reply soon"})
            people.append({"full_name": "Robert Hale", "aliases": [], "role": "landlord"})
        self._send({"message": {"content": json.dumps({"people": people, "events": events})}})


class _Engine:
    """The real casefile service under uvicorn on a free local port, plus an API client with the cookie."""

    def __init__(self, tmp_path, delay=0.0):
        handler = type("Model", (_FakeModel,), {"delay": delay})
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.settings = Settings(
            model=ModelConfig(model="fake", base_url=f"http://127.0.0.1:{self.model.server_port}",
                              api="ollama", num_ctx=8192, timeout=30),
            cache_dir=tmp_path / "parts",
            jobs_dir=tmp_path / "jobs",
            token=TOKEN,
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))  # bound here and handed over, so no other program can take the port
        self.port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.settings), log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the engine did not start")
            time.sleep(0.02)
        self.api = httpx.Client(base_url=self.base, cookies={"casefile_token": TOKEN}, headers=H, timeout=10)

    def close(self):
        self.api.close()
        self.server.should_exit = True
        self.thread.join(10)
        self.model.shutdown()

    def job(self, title, files, start=True):
        job = self.api.post("/api/jobs", json={"title": title, "folder": title}).json()
        form = [("paths", (None, path)) for path in files] + [("files", ("upload", body)) for body in files.values()]
        assert self.api.post(f"/api/jobs/{job['id']}/files", files=form).status_code == 200
        if start:
            assert self.api.post(f"/api/jobs/{job['id']}/start").status_code == 200
        return job["id"]

    def wait(self, job_id, statuses=("done", "failed", "cancelled"), timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.api.get(f"/api/jobs/{job_id}").json()
            if job["status"] in statuses:
                return job
            time.sleep(0.05)
        raise AssertionError(f"job did not reach {statuses}")


@pytest.fixture(scope="module")
def browser():
    try:
        manager = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001 - any failure here means "no browser to test with"
        pytest.skip(f"Playwright can't start: {exc}")
    try:
        chromium = manager.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        manager.stop()
        pytest.skip(f"Playwright's Chromium isn't installed: {exc}")
    yield chromium
    chromium.close()
    manager.stop()


@pytest.fixture
def engine(tmp_path):
    running = _Engine(tmp_path)
    yield running
    running.close()


@pytest.fixture
def slow_engine(tmp_path):
    running = _Engine(tmp_path, delay=1.5)
    yield running
    running.close()


@pytest.fixture
def open_page(browser):
    """Opens a fresh browser that came in through the start-up link, as a user does."""
    contexts = []

    def open_(engine, path="/"):
        context = browser.new_context()
        contexts.append(context)
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())  # "Delete …?" and "Stop …?" confirmations
        page.goto(f"{engine.base}/?token={TOKEN}")
        assert page.url.rstrip("/") == engine.base  # the token is swapped for a cookie and dropped from the address
        if path != "/":
            page.goto(engine.base + path)
        return page

    yield open_
    for context in contexts:
        context.close()


def test_first_visit_explains_how_it_works(open_page, engine):
    page = open_page(engine)
    sync_api.expect(page.get_by_role("heading", name="What happened in this folder?")).to_be_visible()
    sync_api.expect(page.get_by_role("heading", name="How it works")).to_be_visible()
    sync_api.expect(page.get_by_role("button", name="Delete all reports and saved data")).to_have_count(0)


def test_report_page_key_filters_and_quotes(open_page, engine):
    job_id = engine.job("Harbour Road", {"letters/notice.txt": NOTICE, "inspection.txt": INSPECTION, "emails/reply.txt": REPLY})
    assert engine.wait(job_id)["status"] == "done"
    meanings = {k: v["meaning"] for k, v in engine.api.get("/api/config").json()["importance"].items()}

    page = open_page(engine, f"/#/job/{job_id}")
    expect = sync_api.expect
    expect(page.get_by_role("heading", name="Harbour Road")).to_be_visible()

    # The colour key spells out each level in words (it once printed "[object HTMLElement]").
    key = page.locator(".colour-key")
    expect(key.locator("dt")).to_have_text(["Major", "Notable", "Standard", "Minor"])
    expect(key.locator("dd")).to_have_text([meanings[k] for k in ("major", "notable", "standard", "minor")])
    assert "[object" not in key.inner_text()

    log = page.locator(".log")
    rows = log.locator(".ev")
    expect(rows).to_have_count(3)
    # Short lists don't offer "Show 0 more" (a display rule once overrode `hidden`).
    expect(page.locator(".more:visible")).to_have_count(0)

    # Importance chips hide and show their rows.
    minor_chip = page.locator(".chip", has_text="Minor")
    minor_chip.click()
    expect(minor_chip).to_have_attribute("aria-pressed", "false")
    expect(log.locator(".ev.minor")).to_be_hidden()
    expect(log.locator(".ev.major")).to_be_visible()
    minor_chip.click()
    expect(log.locator(".ev.minor")).to_be_visible()

    # Choosing a person shows only their events; the key events section steps aside.
    page.locator(".people button", has_text="Ann").click()
    expect(page.locator(".who-filter")).to_contain_text("Showing events involving Ann")
    expect(log.locator(".ev:visible")).to_have_count(1)
    expect(log.locator(".ev.notable")).to_be_visible()
    expect(page.locator(".key")).to_be_hidden()
    page.get_by_role("button", name="Show everyone").click()
    expect(log.locator(".ev:visible")).to_have_count(3)

    # "Show source" under the date opens the file, place and exact words, in the log and Key events.
    row = log.locator(".ev.major")
    expect(row.locator(".source")).to_be_hidden()
    expect(row.locator(".when").get_by_role("button", name="Show source")).to_be_visible()
    row.get_by_role("button", name="Show source").click()
    expect(row.locator(".source .file")).to_have_text("notice.txt")
    expect(row.locator(".source .where")).to_have_text("line 1")
    expect(row.locator(".source-folder")).to_have_text("in letters")
    expect(row.locator(".source blockquote")).to_contain_text("served notice to end the tenancy")
    expect(row.get_by_role("button", name="Hide source")).to_have_attribute("aria-expanded", "true")
    key_row = page.locator(".key .ev")
    key_row.get_by_role("button", name="Show source").click()
    expect(key_row.locator(".source blockquote")).to_be_visible()


# How deep real archive folders get: "Disclosure/Bundle 3/Correspondence/2021/Q3/…".
DEEP = "/".join(f"Disclosure bundle section {n}" for n in range(8))


def _big_report(events=6000, files=3000, people=250):
    """A report the size of a large case: thousands of events, files and people."""
    levels = ["minor", "standard", "standard", "notable", "major"]
    names = [f"Person{i:03d}" for i in range(people)]
    evs = []
    for i in range(events):
        month = 1 + (i * 24 // events)  # 24 months, 250 events each
        year, mon = 2021 + (month - 1) // 12, (month - 1) % 12 + 1
        evs.append({
            "date": f"{year}-{mon:02d}-{1 + i % 28:02d}", "date_label": f"{1 + i % 28} {mon}/{year}",
            "summary": f"Event number {i} happened.", "quote": f"Quote for event number {i} in the file",
            "source": f"case/box{i % 40:02d}/very/long/folder/name/that/keeps/going/{DEEP}/document-{i % files:05d}.pdf, p. {1 + i % 9}",
            "kind": "message", "icon": "mail", "importance": levels[i % 5], "people": [names[i % people]],
        })
    evs.sort(key=lambda e: e["date"])
    sources = [{"path": f"case/box{i % 40:02d}/document-{i:05d}.pdf", "status": "ok" if i % 10 else "skipped: unsupported file type",
                "events": 2} for i in range(files)]
    counts = {lvl: sum(e["importance"] == lvl for e in evs) for lvl in ("minor", "standard", "notable", "major")}
    return {
        "title": "Big case", "folder": "case", "model": "fake", "generated": "2026-10-22T00:00:00",
        "period": [evs[0]["date"], evs[-1]["date"]], "counts": counts,
        "people": [{"label": n, "full_name": n, "roles": ["witness"], "events": events // people, "active": None,
                    "first_seen": ""} for n in names],
        "events": evs, "unverified": evs[:500], "sources": sources, "failures": [],
    }


def test_a_report_with_thousands_of_events_and_files_stays_light(open_page, engine):
    job_id = engine.job("Big case", {"notice.txt": NOTICE})
    assert engine.wait(job_id)["status"] == "done"
    big = _big_report()
    page = open_page(engine)
    page.route(f"**/api/jobs/{job_id}/report", lambda route: route.fulfill(json=big))
    expect = sync_api.expect
    started = time.time()
    page.goto(f"{engine.base}/#/job/{job_id}")
    expect(page.get_by_role("heading", name="Big case")).to_be_visible()
    assert time.time() - started < 10

    # Months are built only when scrolled near (Key events sit above them): far fewer rows than events.
    first_month = page.locator(".log .month").first
    expect(page.locator(".log .ev")).to_have_count(0)
    first_month.scroll_into_view_if_needed()
    expect(first_month.locator(".ev")).to_have_count(250)
    built = page.locator(".log .ev").count()
    assert 250 <= built < 1500, built
    # Long lists come a page at a time: 1,200 major events, 50 shown in Key events.
    key = page.locator(".key")
    expect(key.locator(".ev")).to_have_count(50)
    key.get_by_role("button", name="Show 50 more key events (1,150 not shown)").click()
    expect(key.locator(".ev")).to_have_count(100)
    # Scrolling builds later months.
    page.locator(".log .month").last.scroll_into_view_if_needed()
    expect(page.locator(".log .month").last.locator(".ev").first).to_be_visible()

    # Filters count months that aren't built yet: hiding everything empties the log.
    for level in ("Major", "Notable", "Standard", "Minor"):
        page.locator(".chip", has_text=level).click()
    expect(page.locator(".log .ev")).to_have_count(0)
    expect(page.get_by_text("Nothing matches. Turn a filter back on.")).to_be_visible()
    page.locator(".chip", has_text="Major").click()
    expect(page.locator(".log .month:visible")).to_have_count(24)

    # Long file paths are cut, not wrapped; the full path is in the tooltip.
    row = page.locator(".log .ev").first
    row.get_by_role("button", name="Show source").click()
    folder = row.locator(".source-folder")
    assert folder.evaluate("el => el.scrollWidth > el.clientWidth")
    assert "very/long/folder" in row.locator(".source-file").get_attribute("title")

    # Files aren't built until opened, then come 200 at a time, searchable, unread ones alone.
    files = page.locator("details", has=page.locator("summary", has_text="Files (3,000, 300 not read)"))
    expect(files.locator(".sources li")).to_have_count(0)
    files.locator("summary").click()
    expect(files.locator(".sources li")).to_have_count(200)
    expect(files.get_by_role("button", name="Show 200 more files (2,800 not shown)")).to_be_visible()
    files.get_by_role("searchbox").fill("document-0042")
    expect(files.locator(".sources li")).to_have_count(10)  # 00420-00429
    files.get_by_role("searchbox").fill("")
    files.get_by_label("Only files that couldn't be read (300)").check()
    expect(files.get_by_role("button", name="Show 100 more files (100 not shown)")).to_be_visible()

    # Who's who: 100 at a time, with a search box once there are many people.
    aside = page.locator(".aside")
    expect(aside.locator(".people li")).to_have_count(100)
    aside.get_by_role("searchbox").fill("person24")
    expect(aside.locator(".people li")).to_have_count(10)


def test_a_running_report_can_be_stopped_then_deleted(open_page, slow_engine):
    files = {f"letters/notice{i}.txt": NOTICE + f" Copy {i}.".encode() for i in range(6)}
    job_id = slow_engine.job("Slow folder", files)
    slow_engine.wait(job_id, ("running",))

    page = open_page(slow_engine, f"/#/job/{job_id}")
    expect = sync_api.expect
    stop = page.get_by_role("button", name="Stop")
    expect(stop).to_be_visible()
    stop.click()  # the confirmation is accepted by open_page
    expect(page.get_by_text("You stopped this report")).to_be_visible(timeout=20000)
    assert slow_engine.api.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"
    expect(page.get_by_role("button", name="Choose the folder again")).to_be_visible()

    page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.get_by_role("heading", name="What happened in this folder?")).to_be_visible()
    assert page.url.endswith("#/")
    assert slow_engine.api.get("/api/jobs").json() == []
    assert not (slow_engine.settings.jobs_dir / job_id).exists()


def test_leaving_a_running_report_is_not_undone_by_its_progress_updates(open_page, slow_engine):
    job_id = slow_engine.job("Slow folder", {f"n{i}.txt": NOTICE + f" Copy {i}.".encode() for i in range(4)})
    page = open_page(slow_engine, f"/#/job/{job_id}")
    sync_api.expect(page.get_by_role("button", name="Stop")).to_be_visible()
    page.locator(".wordmark").click()
    sync_api.expect(page.get_by_role("heading", name="Earlier reports")).to_be_visible()
    page.wait_for_timeout(4500)  # two polling rounds of the progress page
    sync_api.expect(page.get_by_role("heading", name="What happened in this folder?")).to_be_visible()
    assert page.url.endswith("#/")
    slow_engine.api.delete(f"/api/jobs/{job_id}")


def test_reports_can_be_deleted_from_the_start_page(open_page, engine):
    first = engine.job("First folder", {"notice.txt": NOTICE})
    second = engine.job("Second folder", {"reply.txt": REPLY})
    engine.wait(first)
    engine.wait(second)
    assert any(engine.settings.cache_dir.rglob("*.json"))

    page = open_page(engine)
    expect = sync_api.expect
    expect(page.get_by_role("heading", name="Earlier reports")).to_be_visible()
    page.get_by_role("button", name="Delete First folder").click()
    expect(page.get_by_role("link", name="First folder")).to_have_count(0)
    expect(page.get_by_role("link", name="Second folder")).to_be_visible()
    assert [j["id"] for j in engine.api.get("/api/jobs").json()] == [second]

    page.get_by_role("button", name="Delete all reports and saved data").click()
    expect(page.get_by_role("heading", name="How it works")).to_be_visible()
    assert engine.api.get("/api/jobs").json() == []
    assert not any(engine.settings.cache_dir.rglob("*.json"))


def test_without_the_start_up_link_the_page_says_how_to_get_in(browser, open_page, engine):
    page = open_page(engine)
    sync_api.expect(page.get_by_role("heading", name="What happened in this folder?")).to_be_visible()
    page.context.clear_cookies()
    page.evaluate("location.hash = '#/job/nope'")
    sync_api.expect(page.get_by_text(FORBIDDEN)).to_be_visible()

    stranger = browser.new_context().new_page()
    response = stranger.goto(engine.base + "/")
    assert response.status == 403
    sync_api.expect(stranger.get_by_text(FORBIDDEN)).to_be_visible()
    stranger.context.close()
