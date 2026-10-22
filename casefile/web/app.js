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

function show(...nodes) {
  clearInterval(poller);
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
        ? "Everything stays on this computer. casefile reads the folder where it is and never changes it. It reads PDF, Word, email, web pages and text files; photos and other files are listed but not read."
        : "Everything stays on this computer. Reads PDF, Word, email, web pages and text files. Photos and other files are listed but not read."),
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
    ))),
  );
}

function describeStatus(job) {
  const when = new Date(job.created * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  if (job.status === "done") {
    const c = job.counts || {};
    return `${when}. ${plural(c.major || 0, "major event")}.`;
  }
  if (job.status === "failed") return `${when}. Failed.`;
  if (job.status === "running") return `${when}. Reading, ${job.total ? Math.floor((100 * job.done) / job.total) : 0}% done.`;
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

  try {
    const job = await api("POST", "/api/jobs", { title: folder, folder, not_sent: notSent.map((x) => x.path) });
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
    status.replaceWith(el("p", { class: "error" }, `Sending failed: ${error.message}`));
  }
}

// ---------- a job: progress, then the report ----------

async function openJob(id) {
  let job;
  try { job = await api("GET", `/api/jobs/${id}`); } catch (error) {
    show(el("section", { class: "working" }, el("h1", {}, "Report not found"), el("p", {}, error.message),
      el("a", { class: "btn quiet", href: "#/" }, "Start again")));
    return;
  }
  if (job.status === "done") return showReport(job);
  if (job.status === "failed") {
    show(el("section", { class: "working" },
      el("h1", {}, job.title),
      el("p", { class: "error" }, job.error),
      el("button", { class: "btn", type: "button", onclick: chooseFolder }, icon("folder-open"), "Choose the folder again"),
    ));
    return;
  }
  const heading = el("p");
  const bar = el("div");
  const current = el("p", { class: "current" });
  show(el("section", { class: "working", "aria-live": "polite" },
    el("h1", {}, job.title), heading, el("div", { class: "meter" }, bar), current,
    el("p", { class: "note" }, "You can close this page. The report keeps building and will be listed on the start page."),
  ));
  const update = (j) => {
    if (j.status === "queued") {
      heading.textContent = "Waiting for the report before this one to finish.";
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
    current.textContent = j.current ? `Last read: ${j.current}` : "";
  };
  update(job);
  poller = setInterval(async () => {
    try {
      const j = await api("GET", `/api/jobs/${id}`);
      if (j.status === "done" || j.status === "failed") openJob(id);
      else update(j);
    } catch { /* keep polling through a brief hiccup */ }
  }, 2000);
}

function eventRow(ev, { quote = true } = {}) {
  const tag = ev.importance === "major" || ev.importance === "notable" ? el("span", { class: "tag" }, LABEL[ev.importance]) : null;
  const words = quote && ev.quote ? el("blockquote", { hidden: true }, `“${ev.quote}”`) : null;
  const toggle = words
    ? el("button", { class: "linkish", type: "button", "aria-expanded": "false", onclick: (e) => {
        words.hidden = !words.hidden;
        e.currentTarget.setAttribute("aria-expanded", String(!words.hidden));
        e.currentTarget.textContent = words.hidden ? "Show the words" : "Hide the words";
      } }, "Show the words")
    : null;
  return el("li", { class: `ev ${ev.importance}`, "data-people": ev.people.join("\n") },
    mark(ev.icon, ev.importance),
    el("time", { datetime: ev.date || null }, ev.date_label || "No date"),
    el("p", { class: "what" }, ev.summary),
    el("span", {}),
    el("p", { class: "src" }, tag, el("span", {}, ev.source), toggle),
    words,
  );
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
    show(el("p", { class: "error" }, error.message));
    return;
  }
  const visible = new Set(LEVELS);
  let person = null;

  // ---- the log, grouped by month ----
  const groups = new Map();
  for (const ev of rep.events) {
    const key = ev.date ? ev.date.slice(0, 7) : "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ev);
  }
  const months = [...groups].map(([key, evs]) =>
    el("section", { class: "month" }, el("h2", {}, monthName(key)), el("ul", { class: "events" }, evs.map((ev) => eventRow(ev)))));
  const log = el("section", { class: "log", "aria-label": "What happened" },
    months.length ? months : el("p", { class: "empty" }, "No events were found in these files."));
  const empty = el("p", { class: "empty", hidden: true }, "Nothing matches. Turn a filter back on.");

  const major = rep.events.filter((ev) => ev.importance === "major");
  const key = major.length
    ? el("section", { class: "key", "aria-labelledby": "key-h" },
        el("h2", { id: "key-h" }, `Key events (${major.length})`),
        el("ul", { class: "events" }, major.map((ev) => eventRow(ev, { quote: false }))))
    : null;

  // ---- filters: importance chips double as the legend ----
  const whoLine = el("p", { class: "who-filter", hidden: true });
  function applyFilters() {
    for (const row of log.querySelectorAll(".ev")) {
      const level = LEVELS.find((l) => row.classList.contains(l));
      const involved = !person || row.dataset.people.split("\n").includes(person);
      row.hidden = !(visible.has(level) && involved);
    }
    let any = false;
    for (const month of log.querySelectorAll(".month")) {
      const shown = month.querySelector(".ev:not([hidden])") !== null;
      month.hidden = !shown;
      any ||= shown;
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

  // ---- who's who ----
  const personButtons = new Map();
  function choosePerson(label) {
    person = person === label ? null : label;
    for (const [name, button] of personButtons) button.setAttribute("aria-pressed", String(name === person));
    applyFilters();
  }
  const people = el("ul", { class: "people" }, rep.people.map((p) => {
    const button = el("button", { type: "button", "aria-pressed": "false", onclick: () => choosePerson(p.label),
      title: p.full_name !== p.label ? p.full_name : null },
      el("span", { class: "name" }, p.label),
      el("span", { class: "count" }, p.events),
      el("span", { class: "role" }, [p.full_name !== p.label ? p.full_name : null, ...p.roles].filter(Boolean).join(", ")));
    personButtons.set(p.label, button);
    return el("li", {}, button);
  }));

  const period = rep.period ? `${rep.events.find((e) => e.date).date_label} to ${[...rep.events].reverse().find((e) => e.date).date_label}. ` : "";
  const filesRead = rep.sources.filter((s) => s.status === "ok").length;

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
        "Under each event, “Show the words” gives the exact text it came from and the file it's in. "
        + "The AI can misread things, so check anything important there."),
      log,
      empty,
      rep.unverified.length ? el("details", {},
        el("summary", {}, `Needs checking (${rep.unverified.length})`),
        el("p", { class: "note" }, "The AI reported these, but the words it quoted were not found in the file. Treat them as leads, not facts."),
        el("ul", { class: "events" }, rep.unverified.map((ev) => eventRow(ev)))) : null,
      el("details", {},
        el("summary", {}, `Files (${rep.sources.length})`),
        el("ul", { class: "sources" }, rep.sources.map((s) => el("li", {},
          el("span", {}, s.path),
          el("span", { class: "status" }, s.status === "ok" ? plural(s.events, "event") : s.status))))),
      el("p", { class: "note" },
        `Written by ${rep.model}, an AI model running on this computer. It can miss or misread things: check the source before relying on an entry. `,
        el("button", { class: "linkish", type: "button", onclick: async () => {
          if (!confirm(`Delete the report “${rep.title}”?`)) return;
          await api("DELETE", `/api/jobs/${job.id}`);
          location.hash = "#/";
        } }, "Delete this report")),
    ),
    el("aside", { class: "aside", "aria-labelledby": "who-h" },
      el("h2", { id: "who-h" }, "Who’s who"),
      el("p", { class: "note" }, "Choose a person to see only their events."),
      people),
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
  CONFIG = await api("GET", "/api/config");
  checkModel();
  setInterval(checkModel, 30000);
  window.addEventListener("hashchange", route);
  route();
})();
