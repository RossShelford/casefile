"use strict";

const $app = document.getElementById("app");
const $model = document.getElementById("model");
const $picker = document.getElementById("picker");
const LEVELS = ["major", "notable", "standard", "minor"];
const LABEL = { major: "Major", notable: "Notable", standard: "Standard", minor: "Minor" };
const BATCH_FILES = 40;
const BATCH_BYTES = 25 * 1024 * 1024;
// Present only inside the desktop app (see desktop/preload.js).
const DESKTOP = window.casefileDesktop || null;
let CONFIG = null;
let poller = null;

// ---------- small helpers ----------

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid instanceof Node ? kid : String(kid));
  return node;
}

function icon(name) {
  const node = el("span", { class: "ico", "aria-hidden": "true" });
  node.style.setProperty("--i", `url(/assets/icons/${name}.svg)`); // CSSOM, so the page's CSP can forbid inline styles
  return node;
}

function mark(iconName, importance) {
  return el("span", { class: `mark ${importance}`, title: LABEL[importance] }, icon(iconName));
}

async function api(method, path, body) {
  const options = { method, headers: { "X-Casefile": "1" } };
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  // The engine only answers the browser that opened its start-up link (which sets a cookie).
  if (response.status === 403) throw new Error("Open casefile from the link printed when it started.");
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function plural(n, word) { return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

function aboutTime(seconds) {
  if (seconds == null) return "";
  if (seconds < 90) return "About a minute left.";
  if (seconds < 3600) return `About ${Math.round(seconds / 60)} minutes left.`;
  const hours = seconds / 3600;
  return `About ${hours < 10 ? hours.toFixed(1) : Math.round(hours)} hours left.`;
}

function stopPolling() {
  clearInterval(poller);
  poller = null;
}

function show(...nodes) {
  stopPolling();
  $app.replaceChildren(...nodes);
  $app.focus();
}

// ---------- model status ----------

async function checkModel() {
  try {
    const health = await api("GET", "/api/health");
    $model.textContent = health.message;
    $model.classList.toggle("bad", !health.ok);
    return health.ok;
  } catch (error) {
    $model.textContent = `Can't reach casefile: ${error.message}`;
    $model.classList.add("bad");
    return false;
  }
}

// ---------- choosing a folder ----------

async function chooseFolder() {
  if (!DESKTOP) {
    $picker.click(); // browser: the page reads the folder and sends the files
    return;
  }
  const path = await DESKTOP.pickFolder(); // desktop: the folder is read where it is
  if (!path) return;
  try {
    const job = await api("POST", "/api/jobs/local", { path });
    location.hash = `#/job/${job.id}`;
  } catch (error) {
    show(el("section", { class: "working" }, el("h1", {}, "That folder couldn't be opened"), el("p", { class: "error" }, error.message),
      el("button", { class: "btn", type: "button", onclick: chooseFolder }, icon("folder-open"), "Choose another folder")));
  }
}

// ---------- first run: get the AI model ready ----------

function gigabytes(bytes) { return `${(bytes / 1e9).toFixed(1)} GB`; }

async function renderSetup(box, chooseButton) {
  let health;
  try { health = await api("GET", "/api/health"); } catch (error) { health = { ok: false, problem: "server", message: error.message }; }
  chooseButton.disabled = !health.ok;
  box.hidden = health.ok;
  if (health.ok) return;
  const recheck = el("button", { class: "btn quiet", type: "button", onclick: async (e) => {
    e.currentTarget.disabled = true;
    e.currentTarget.textContent = "Checking…";
    if (DESKTOP) await DESKTOP.startModelServer();
    await checkModel();
    renderSetup(box, chooseButton);
  } }, "Check again");

  if (health.problem === "server") {
    box.replaceChildren(
      el("h2", {}, "First, install Ollama"),
      el("p", {}, "casefile uses Ollama, a free app, to run the AI model on this computer. Install it, open it once, then come back here."),
      el("div", { class: "setup-actions" },
        DESKTOP
          ? el("button", { class: "btn", type: "button", onclick: () => DESKTOP.openOllamaDownload() }, icon("download"), "Download Ollama")
          : el("a", { class: "btn", href: "https://ollama.com/download", target: "_blank", rel: "noopener" }, icon("download"), "Download Ollama"),
        recheck),
      el("p", { class: "note" }, health.message),
    );
    return;
  }
  if (!CONFIG.can_download_model) {
    box.replaceChildren(el("h2", {}, "Load the AI model"), el("p", {}, health.message), recheck);
    return;
  }
  const bar = el("div");
  const status = el("p", { class: "current" });
  const meter = el("div", { class: "meter", hidden: true }, bar);
  const start = el("button", { class: "btn", type: "button", onclick: async () => {
    start.disabled = true;
    meter.hidden = false;
    try { await api("POST", "/api/model/pull"); } catch (error) { status.textContent = error.message; start.disabled = false; return; }
    const timer = setInterval(async () => {
      const pull = await api("GET", "/api/model/pull").catch(() => null);
      if (!pull) return;
      if (pull.total) {
        bar.style.width = `${(100 * pull.completed) / pull.total}%`;
        status.textContent = `${gigabytes(pull.completed)} of ${gigabytes(pull.total)} downloaded.`;
      }
      if (pull.status === "done") {
        clearInterval(timer);
        await checkModel();
        renderSetup(box, chooseButton);
      } else if (pull.status === "failed") {
        clearInterval(timer);
        status.textContent = `The download stopped: ${pull.error}`;
        start.disabled = false;
        start.textContent = "Try again";
      }
    }, 1000);
  } }, icon("download"), "Download the AI model");
  box.replaceChildren(
    el("h2", {}, "Next, download the AI model"),
    el("p", {}, `casefile needs ${CONFIG.model}. It's about 4.7 GB and only downloads once. Keep this window open until it finishes.`),
    start, meter, status,
  );
}

// ---------- start page ----------

async function home() {
  const past = el("section", { class: "past", "aria-labelledby": "past-h" });
  const choose = el("button", { class: "btn", type: "button", onclick: chooseFolder, disabled: true }, icon("folder-open"), "Choose folder");
  const setup = el("section", { class: "setup", hidden: true, "aria-live": "polite" });
  show(
    el("section", { class: "start" },
      el("h1", {}, "What happened in this folder?"),
      el("p", { class: "lede" },
        "Choose a folder of documents. You get back one list of events in date order, the important ones marked in red and amber."),
      setup,
      choose,
      el("p", { class: "note" }, DESKTOP
        ? "Everything stays on this computer. casefile reads the folder where it is and never changes it. It reads PDF, Word, Excel, PowerPoint, emails (including Outlook files and attachments), web pages and text; photos and other files are listed but not read."
        : "Everything stays on this computer. Reads PDF, Word, Excel, PowerPoint, emails (including Outlook files and attachments), web pages and text. Photos and other files are listed but not read."),
    ),
    past,
  );
  renderSetup(setup, choose);
  let jobs = [];
  try { jobs = await api("GET", "/api/jobs"); } catch { /* the start page still works */ }
  if (!jobs.length) {
    // First visit: what to expect. Replaced by the list of reports once there is one.
    past.append(
      el("h2", { id: "past-h" }, "How it works"),
      el("ol", { class: "howto" },
        el("li", {}, el("strong", {}, "Choose a folder. "),
          "casefile reads every document in it, including folders inside it."),
        el("li", {}, el("strong", {}, "Leave it running. "),
          "A handful of files takes a few minutes. A large folder can take many hours, so it's fine to start it and come back. Each step is saved, so stopping and starting again picks up where it left off."),
        el("li", {}, el("strong", {}, "Read what happened. "),
          "Events are listed by date. Major events are red and listed first, notable ones amber. Choose a person to see only their events."),
      ),
    );
    return;
  }
  past.append(
    el("h2", { id: "past-h" }, "Earlier reports"),
    el("ul", {}, jobs.map((job) => el("li", {},
      el("a", { href: `#/job/${job.id}` }, job.title),
      el("span", { class: "meta" }, describeStatus(job)),
      el("button", { class: "linkish", type: "button", "aria-label": `Delete ${job.title}`, onclick: async (e) => {
        if (!(await deleteJob(job))) return;
        e.target.closest("li").remove();
        if (!past.querySelector("li")) home();
      } }, "Delete"),
    ))),
    el("p", { class: "note" },
      el("button", { class: "linkish", type: "button", onclick: deleteEverything }, "Delete all reports and saved data")),
  );
}

// Deleting removes the report, any copies of the documents and the saved model results for it.
async function deleteJob(job) {
  if (!confirm(`Delete “${job.title}”? This removes the report and everything casefile saved while reading the folder.`)) return false;
  try {
    await api("DELETE", `/api/jobs/${job.id}`);
    return true;
  } catch (error) {
    alert(`It couldn't be deleted: ${error.message}`);
    return false;
  }
}

async function deleteEverything() {
  if (!confirm("Delete every report and everything casefile has saved? Reports still being built are stopped first. Your own folders are not touched.")) return;
  try { await api("DELETE", "/api/data"); } catch (error) { alert(`Not everything could be deleted: ${error.message}`); }
  home();
}

function describeStatus(job) {
  const when = new Date(job.created * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  if (job.status === "done") {
    const c = job.counts || {};
    return `${when}. ${plural(c.major || 0, "major event")}.`;
  }
  if (job.status === "failed") return `${when}. Failed.`;
  if (job.status === "cancelled") return `${when}. Stopped.`;
  if (job.status === "running") return `${when}. Reading, ${job.total ? Math.floor((100 * job.done) / job.total) : 0}% done.`;
  if (job.status === "uploading") return `${when}. Still being sent.`;
  return `${when}. Waiting to start.`;
}

// ---------- choosing and sending a folder ----------

$picker.addEventListener("change", async () => {
  const files = [...$picker.files];
  $picker.value = "";
  if (files.length) await sendFolder(files);
});

async function sendFolder(files) {
  const folder = files[0].webkitRelativePath.split("/")[0] || "Folder";
  const send = [];
  const notSent = [];
  for (const file of files) {
    const path = file.webkitRelativePath.split("/").slice(1).join("/") || file.name;
    if (path.split("/").some((part) => part.startsWith("."))) continue; // hidden system files
    const dot = path.lastIndexOf(".");
    const ext = dot === -1 ? "" : path.slice(dot).toLowerCase();
    (CONFIG.extensions.includes(ext) ? send : notSent).push({ file, path });
  }

  const status = el("p", { class: "current" });
  const bar = el("div");
  show(el("section", { class: "working", "aria-live": "polite" },
    el("h1", {}, folder),
    el("p", {}, `Sending ${plural(send.length, "file")} to casefile on this computer.`),
    el("div", { class: "meter" }, bar),
    status,
  ));
  if (!send.length) {
    status.replaceWith(el("p", { class: "error" },
      "None of these files can be read. casefile reads PDF, Word (.docx), email (.eml), web pages and text files."),
      el("button", { class: "btn quiet", type: "button", onclick: chooseFolder }, "Choose another folder"));
    return;
  }

  let job = null;
  try {
    job = await api("POST", "/api/jobs", { title: folder, folder, not_sent: notSent.map((x) => x.path) });
    let sent = 0;
    for (let i = 0; i < send.length;) {
      const form = new FormData();
      let bytes = 0;
      const start = i;
      while (i < send.length && i - start < BATCH_FILES && (i === start || bytes + send[i].file.size <= BATCH_BYTES)) {
        form.append("files", send[i].file, "upload");
        form.append("paths", send[i].path);
        bytes += send[i].file.size;
        i++;
      }
      await api("POST", `/api/jobs/${job.id}/files`, form);
      sent = i;
      bar.style.width = `${(100 * sent) / send.length}%`;
      status.textContent = `${sent.toLocaleString()} of ${send.length.toLocaleString()} sent.`;
    }
    await api("POST", `/api/jobs/${job.id}/start`);
    location.hash = `#/job/${job.id}`;
  } catch (error) {
    // Don't leave a half-sent copy of the folder behind: stop and delete the unfinished report.
    if (job) {
      await api("POST", `/api/jobs/${job.id}/cancel`).catch(() => null);
      await api("DELETE", `/api/jobs/${job.id}`).catch(() => null);
    }
    status.replaceWith(
      el("p", { class: "error" }, `Sending failed: ${error.message}`),
      el("p", {}, "What was sent so far has been deleted."),
      el("div", { class: "row-actions" },
        el("button", { class: "btn", type: "button", onclick: chooseFolder }, icon("folder-open"), "Choose the folder again"),
        el("a", { class: "btn quiet", href: "#/" }, "Back to the start")),
    );
  }
}

// ---------- a job: progress, then the report ----------

function onJobPage(id) { return location.hash === `#/job/${id}`; }

const ENDED = ["done", "failed", "cancelled"];

async function openJob(id) {
  stopPolling();
  let job;
  try { job = await api("GET", `/api/jobs/${id}`); } catch (error) {
    if (!onJobPage(id)) return;
    show(el("section", { class: "working" }, el("h1", {}, "Report not found"), el("p", {}, error.message),
      el("a", { class: "btn quiet", href: "#/" }, "Start again")));
    return;
  }
  if (!onJobPage(id)) return; // the user moved on while this was loading
  if (job.status === "done") return showReport(job);
  if (job.status === "failed" || job.status === "cancelled") {
    show(el("section", { class: "working" },
      el("h1", {}, job.title),
      job.status === "failed"
        ? el("p", { class: "error" }, job.error)
        : el("p", {}, "You stopped this report before it finished. What was read so far is saved, so choosing the folder again picks up where it left off."),
      el("div", { class: "row-actions" },
        el("button", { class: "btn", type: "button", onclick: chooseFolder }, icon("folder-open"), "Choose the folder again"),
        el("button", { class: "btn quiet", type: "button", onclick: async () => {
          if (await deleteJob(job)) location.hash = "#/";
        } }, "Delete")),
    ));
    return;
  }
  const heading = el("p");
  const bar = el("div");
  const current = el("p", { class: "current" });
  let stopping = false;
  const cancel = el("button", { class: "btn quiet", type: "button", onclick: async () => {
    if (!confirm("Stop reading this folder? What was read so far is saved, so choosing the folder again picks up where it left off.")) return;
    cancel.disabled = true;
    try {
      await api("POST", `/api/jobs/${id}/cancel`);
      stopping = true;
      cancel.textContent = "Stopping…";
      heading.textContent = "Stopping after the part being read now.";
    } catch (error) {
      cancel.disabled = false;
      current.textContent = `It couldn't be stopped: ${error.message}`;
    }
  } }, "Stop");
  show(el("section", { class: "working", "aria-live": "polite" },
    el("h1", {}, job.title), heading, el("div", { class: "meter" }, bar), current,
    el("div", { class: "row-actions" }, cancel),
    el("p", { class: "note" }, "You can close this page. The report keeps building and will be listed on the start page."),
  ));
  const update = (j) => {
    if (stopping) {
      heading.textContent = "Stopping after the part being read now.";
    } else if (j.status === "queued") {
      heading.textContent = "Waiting for the report before this one to finish.";
    } else if (j.status === "uploading") {
      heading.textContent = "The files are still being sent.";
    } else if (!j.total) {
      heading.textContent = "Looking through the files.";
    } else if (!j.done) {
      // No measured speed yet: give a rough figure so a long first step doesn't look frozen.
      const minutes = j.total; // about a minute per part on a typical laptop
      const rough = minutes < 90 ? `${plural(minutes, "minute")}` : `${Math.round(minutes / 60)} hours`;
      heading.textContent = `${plural(j.total, "part")} to read. On a typical laptop each takes about a minute, so roughly ${rough}. `
        + "A closer estimate appears after the first part.";
    } else {
      heading.textContent = `${j.done.toLocaleString()} of ${plural(j.total, "part")} read. ${aboutTime(j.eta_seconds)}`;
    }
    bar.style.width = j.total ? `${(100 * j.done) / j.total}%` : "0";
    if (!current.textContent.startsWith("It couldn't")) current.textContent = j.current ? `Last read: ${j.current}` : "";
  };
  update(job);
  const mine = setInterval(async () => {
    let j;
    try { j = await api("GET", `/api/jobs/${id}`); } catch { return; /* keep polling through a brief hiccup */ }
    // A reply that arrives after the user left this page (or after a newer view started) must not redraw.
    if (poller !== mine || !onJobPage(id)) return;
    if (ENDED.includes(j.status)) openJob(id);
    else update(j);
  }, 2000);
  poller = mine;
}

// Sources look like "folder/file.pdf, p. 3" or "mail/a.eml > notice.pdf, line 4".
function splitSource(source) {
  const cut = source.lastIndexOf(", ");
  const path = cut === -1 ? source : source.slice(0, cut);
  const where = cut === -1 ? "" : source.slice(cut + 2);
  const slash = path.lastIndexOf("/");
  return { folder: slash === -1 ? "" : path.slice(0, slash), file: slash === -1 ? path : path.slice(slash + 1), where };
}

let sourceCount = 0;

// One event: what happened reads on its own; where it came from stays one click away
// ("Show source" under the date), so thousands of file names don't crowd the log.
function eventRow(ev) {
  const id = `source-${++sourceCount}`;
  const { folder, file, where } = splitSource(ev.source);
  const panel = el("div", { class: "source", id, hidden: true },
    el("p", { class: "source-file", title: ev.source },
      el("span", { class: "file" }, file), where ? el("span", { class: "where" }, where) : null),
    folder ? el("p", { class: "source-folder", title: folder }, `in ${folder}`) : null,
    ev.quote ? el("blockquote", {}, `“${ev.quote}”`) : null,
  );
  const toggle = el("button", {
    class: "show-source", type: "button", "aria-expanded": "false", "aria-controls": id,
    onclick: (e) => {
      panel.hidden = !panel.hidden;
      e.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
      e.currentTarget.textContent = panel.hidden ? "Show source" : "Hide source";
    },
  }, "Show source");
  const level = ev.importance === "major" || ev.importance === "notable"
    ? el("span", { class: "sr-only" }, `${LABEL[ev.importance]}: `) : null;
  return el("li", { class: `ev ${ev.importance}` },
    mark(ev.icon, ev.importance),
    el("div", { class: "when" }, el("time", { datetime: ev.date || null }, ev.date_label || "No date"), toggle),
    el("p", { class: "what" }, level, ev.summary),
    panel,
  );
}

// A list that shows `page` items at a time with a "Show more" button: a report can hold
// tens of thousands of events or files, and building them all at once freezes the page.
function pagedList(items, renderItem, { page = 200, className = "events", noun = "more" } = {}) {
  const list = el("ul", { class: className });
  const more = el("button", { class: "more", type: "button" });
  let shown = 0;
  function addPage() {
    const next = items.slice(shown, shown + page);
    list.append(...next.map(renderItem));
    shown += next.length;
    const left = items.length - shown;
    more.hidden = left <= 0;
    more.textContent = `Show ${Math.min(page, left).toLocaleString()} ${noun} (${left.toLocaleString()} not shown)`;
  }
  more.addEventListener("click", addPage);
  addPage();
  return el("div", { class: "paged" }, list, more);
}

function searchBox(label, onInput) {
  let timer = null;
  const input = el("input", { type: "search", class: "search", "aria-label": label, placeholder: label });
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => onInput(input.value.trim().toLocaleLowerCase()), 150);
  });
  return input;
}

function monthName(key) {
  if (!key) return "No date given";
  if (key.length === 4) return key;
  const [y, m] = key.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

async function showReport(job) {
  let rep;
  try { rep = await api("GET", `/api/jobs/${job.id}/report`); } catch (error) {
    if (onJobPage(job.id)) show(el("p", { class: "error" }, error.message));
    return;
  }
  if (!onJobPage(job.id)) return;
  const visible = new Set(LEVELS);
  let person = null;
  const matches = (ev) => visible.has(ev.importance) && (!person || ev.people.includes(person));

  // ---- the log, grouped by month, each month built only when it scrolls near ----
  const groups = new Map();
  for (const ev of rep.events) {
    const key = ev.date ? ev.date.slice(0, 7) : "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ev);
  }
  const months = [...groups].map(([key, events]) => {
    const body = el("div");
    const section = el("section", { class: "month" }, el("h2", {}, monthName(key)), body);
    return { events, section, body, built: false };
  });
  const ROW_HEIGHT = 64; // rough height of one event, to reserve space before a month is built
  function buildMonth(month) {
    month.built = true;
    month.section.style.removeProperty("min-height");
    month.body.replaceChildren(pagedList(month.events.filter(matches), eventRow, { page: 300, noun: "more events" }));
  }
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const month = months.find((m) => m.section === entry.target);
      if (entry.isIntersecting && month && !month.built) buildMonth(month);
    }
  }, { rootMargin: "1500px 0px" });
  months.forEach((month) => observer.observe(month.section));
  const log = el("section", { class: "log", "aria-label": "What happened" },
    months.length ? months.map((m) => m.section) : el("p", { class: "empty" }, "No events were found in these files."));
  const empty = el("p", { class: "empty", hidden: true }, "Nothing matches. Turn a filter back on.");

  const major = rep.events.filter((ev) => ev.importance === "major");
  const key = major.length
    ? el("section", { class: "key", "aria-labelledby": "key-h" },
        el("h2", { id: "key-h" }, `Key events (${major.length.toLocaleString()})`),
        pagedList(major, eventRow, { page: 50, noun: "more key events" }))
    : null;

  // ---- filters work on the data, so months not yet built are counted correctly ----
  const whoLine = el("p", { class: "who-filter", hidden: true });
  function applyFilters() {
    let any = false;
    for (const month of months) {
      const count = month.events.reduce((n, ev) => n + (matches(ev) ? 1 : 0), 0);
      month.section.hidden = count === 0;
      any ||= count > 0;
      if (month.built) buildMonth(month);
      else month.section.style.setProperty("min-height", `${Math.min(count, 300) * ROW_HEIGHT + 60}px`);
    }
    empty.hidden = any || !rep.events.length;
    if (key) key.hidden = person !== null || !visible.has("major");
    whoLine.hidden = !person;
    if (person) whoLine.replaceChildren(`Showing events involving ${person}. `,
      el("button", { class: "linkish", type: "button", onclick: () => choosePerson(null) }, "Show everyone"));
  }
  const chips = LEVELS.map((level) => el("button", {
    class: "chip", type: "button", "aria-pressed": "true",
    onclick: (e) => {
      if (visible.has(level)) visible.delete(level); else visible.add(level);
      e.currentTarget.setAttribute("aria-pressed", String(visible.has(level)));
      applyFilters();
    },
  }, mark("circle-dot", level), LABEL[level], el("span", { class: "n" }, rep.counts[level].toLocaleString())));
  const colourKey = el("dl", { class: "colour-key" }, LEVELS.flatMap((level) => [
    el("dt", {}, mark("circle-dot", level), LABEL[level]),
    el("dd", {}, CONFIG.importance[level].meaning),
  ]));

  // ---- who's who: searchable, shown 100 at a time ----
  const personButtons = new Map();
  function choosePerson(label) {
    person = person === label ? null : label;
    for (const [name, button] of personButtons) button.setAttribute("aria-pressed", String(name === person));
    applyFilters();
  }
  function personRow(p) {
    const button = el("button", { type: "button", "aria-pressed": String(p.label === person), onclick: () => choosePerson(p.label),
      title: p.full_name !== p.label ? p.full_name : null },
      el("span", { class: "name" }, p.label),
      el("span", { class: "count" }, p.events.toLocaleString()),
      el("span", { class: "role" }, [p.full_name !== p.label ? p.full_name : null, ...p.roles].filter(Boolean).join(", ")));
    personButtons.set(p.label, button);
    return el("li", {}, button);
  }
  const peopleBox = el("div");
  const showPeople = (query) => {
    personButtons.clear();
    const found = query
      ? rep.people.filter((p) => `${p.label} ${p.full_name} ${p.roles.join(" ")}`.toLocaleLowerCase().includes(query))
      : rep.people;
    peopleBox.replaceChildren(found.length
      ? pagedList(found, personRow, { page: 100, className: "people", noun: "more people" })
      : el("p", { class: "note" }, "Nobody matches."));
  };
  showPeople("");

  // ---- files: built only when opened; searchable; unread files can be listed alone ----
  const unread = rep.sources.filter((s) => s.status !== "ok");
  const sourceRow = (s) => el("li", {},
    el("span", { class: "path", title: s.path }, s.path),
    el("span", { class: "status" }, s.status === "ok" ? plural(s.events, "event") : s.status));
  const filesBox = el("div");
  let onlyUnread = false;
  let fileQuery = "";
  const showFiles = () => {
    const pool = onlyUnread ? unread : rep.sources;
    const found = fileQuery ? pool.filter((s) => s.path.toLocaleLowerCase().includes(fileQuery)) : pool;
    filesBox.replaceChildren(found.length
      ? pagedList(found, sourceRow, { page: 200, className: "sources", noun: "more files" })
      : el("p", { class: "note" }, "No file matches."));
  };
  const unreadToggle = unread.length
    ? el("label", { class: "check" },
        el("input", { type: "checkbox", onchange: (e) => { onlyUnread = e.currentTarget.checked; showFiles(); } }),
        ` Only files that couldn't be read (${unread.length.toLocaleString()})`)
    : null;
  const files = el("details", {},
    el("summary", {}, `Files (${rep.sources.length.toLocaleString()}${unread.length ? `, ${unread.length.toLocaleString()} not read` : ""})`),
    el("div", { class: "list-tools" }, searchBox("Find a file", (q) => { fileQuery = q; showFiles(); }), unreadToggle),
    filesBox);
  files.addEventListener("toggle", () => { if (files.open && !filesBox.firstChild) showFiles(); });

  const checking = rep.unverified.length ? el("details", {},
    el("summary", {}, `Needs checking (${rep.unverified.length.toLocaleString()})`),
    el("p", { class: "note" }, "The AI reported these, but the words it quoted were not found in the file. Treat them as leads, not facts."))
    : null;
  if (checking) checking.addEventListener("toggle", () => {
    if (checking.open && checking.children.length === 2) checking.append(pagedList(rep.unverified, eventRow, { page: 100, noun: "more" }));
  });

  const period = rep.period ? `${rep.events.find((e) => e.date).date_label} to ${[...rep.events].reverse().find((e) => e.date).date_label}. ` : "";
  const filesRead = rep.sources.length - unread.length;

  show(el("div", { class: "report" },
    el("div", {},
      el("div", { class: "head" },
        el("h1", {}, rep.title),
        el("p", { class: "period" }, `${period}${plural(rep.events.length, "event")} from ${plural(filesRead, "file")}.`),
        el("div", { class: "actions" },
          el("a", { class: "btn", href: `/api/jobs/${job.id}/report.pdf`, download: "" }, icon("download"), "Download PDF"),
          el("button", { class: "btn quiet", type: "button", onclick: chooseFolder }, icon("folder-open"), "Choose another folder"),
        ),
      ),
      el("fieldset", { class: "filters" }, el("legend", {}, "Show"), chips),
      colourKey,
      whoLine,
      key,
      el("p", { class: "note words-hint" },
        "“Show source” under a date gives the file, the page or line, and the exact words the event came from. "
        + "The AI can misread things, so check anything important there."),
      log,
      empty,
      checking,
      files,
      el("p", { class: "note" },
        `Written by ${rep.model}, an AI model running on this computer. It can miss or misread things: check the source before relying on an entry. `,
        el("button", { class: "linkish", type: "button", onclick: async () => {
          if (await deleteJob({ id: job.id, title: rep.title })) location.hash = "#/";
        } }, "Delete this report")),
    ),
    el("aside", { class: "aside", "aria-labelledby": "who-h" },
      el("h2", { id: "who-h" }, "Who’s who"),
      el("p", { class: "note" }, "Choose a person to see only their events."),
      rep.people.length > 15 ? searchBox(`Find one of ${rep.people.length.toLocaleString()} people`, showPeople) : null,
      peopleBox),
  ));
  applyFilters();
}

// ---------- routing ----------

function route() {
  const match = location.hash.match(/^#\/job\/([\w-]+)$/);
  if (match) openJob(match[1]);
  else home();
}

(async () => {
  try { CONFIG = await api("GET", "/api/config"); } catch (error) {
    $app.replaceChildren(el("section", { class: "working" }, el("h1", {}, "casefile can't open"), el("p", { class: "error" }, error.message)));
    return;
  }
  checkModel();
  setInterval(checkModel, 30000);
  window.addEventListener("hashchange", route);
  route();
})();
