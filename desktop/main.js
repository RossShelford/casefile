"use strict";
// casefile desktop: starts the bundled engine (the Python service, frozen with PyInstaller) on a
// private local port, makes sure Ollama is running, and shows the engine's page in a window.

const { app, BrowserWindow, dialog, ipcMain, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { pathToFileURL } = require("node:url");

const TOKEN = crypto.randomBytes(32).toString("hex"); // new every launch; only this window gets it
const OLLAMA_URL = "http://127.0.0.1:11434";
const OLLAMA_DOWNLOAD = "https://ollama.com/download";
const STARTING_PAGE = path.join(__dirname, "starting.html");
const STARTING_URL = pathToFileURL(STARTING_PAGE).href;
const LOG_LIMIT = 5 * 1024 * 1024; // engine.log is rotated at startup past this size
// The engine picks its own free port (no race with other programs) and prints it on this line.
const LISTENING = /^casefile: listening on 127\.0\.0\.1:(\d+)\s*$/;

let engine = null;
let ollama = null; // only set if we started Ollama, so we only stop what we started
let mainWindow = null;
let base = null;
let quitting = false;

if (!app.requestSingleInstanceLock()) app.quit();
app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function logStream(name) {
  const dir = app.getPath("logs");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, name);
  // Keep the log from growing forever: one previous copy is enough to report a problem.
  try {
    if (fs.statSync(file).size > LOG_LIMIT) fs.renameSync(file, `${file}.1`);
  } catch {
    /* no log yet */
  }
  return fs.createWriteStream(file, { flags: "a" });
}

function engineCommand() {
  if (app.isPackaged) {
    const exe = process.platform === "win32" ? "casefile-engine.exe" : "casefile-engine";
    return { command: path.join(process.resourcesPath, "engine", exe), args: [] };
  }
  // Development (`npm start`): run the Python package from the repo's virtualenv.
  const repo = path.join(__dirname, "..");
  const venv = process.platform === "win32" ? path.join(".venv", "Scripts", "python.exe") : path.join(".venv", "bin", "python");
  return { command: process.env.CASEFILE_PYTHON || path.join(repo, venv), args: ["-m", "casefile"], cwd: repo };
}

async function waitFor(url, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      if ((await fetch(url)).ok) return true;
    } catch {
      /* not up yet */
    }
    await delay(250);
  }
  return false;
}

async function startEngine() {
  const data = path.join(app.getPath("userData"), "data");
  const { command, args, cwd } = engineCommand();
  const log = logStream("engine.log");
  engine = spawn(
    command,
    [...args, "serve", "--desktop", "--port", "0", "--url", OLLAMA_URL,
      "--data", path.join(data, "jobs"), "--cache", path.join(data, "parts")],
    // The engine exits when its stdin closes, so it never outlives the app, even after a crash.
    { cwd, env: { ...process.env, CASEFILE_TOKEN: TOKEN }, stdio: ["pipe", "pipe", "pipe"], windowsHide: true },
  );
  engine.stderr.pipe(log);
  // Every stdout line goes to the log; the first "listening" line also tells us the engine's port.
  let found;
  const listening = new Promise((resolve) => { found = resolve; });
  readline.createInterface({ input: engine.stdout, crlfDelay: Infinity }).on("line", (line) => {
    log.write(`${line}\n`);
    const match = base ? null : LISTENING.exec(line);
    if (match) {
      base = `http://127.0.0.1:${match[1]}`;
      found(null);
    }
  });
  const failed = new Promise((resolve) => {
    engine.on("error", (error) => resolve(`could not start ${command}: ${error.message}`));
    engine.on("exit", (code) => {
      if (!quitting && mainWindow && base) {
        dialog.showErrorBox("casefile stopped", `The casefile engine stopped unexpectedly (code ${code}).\n\nDetails: ${app.getPath("logs")}`);
        app.quit();
      }
      resolve(`exited with code ${code}`);
    });
  });
  // First launch can be slow: the operating system checks the new engine before running it.
  let timer;
  const late = new Promise((resolve) => { timer = setTimeout(() => resolve("did not start within 2 minutes"), 120000); });
  const problem = await Promise.race([listening, failed, late]);
  clearTimeout(timer);
  if (problem) throw new Error(`The casefile engine ${problem}.\n\nDetails: ${app.getPath("logs")}`);
}

function findOllama() {
  // Apps opened from the Dock/Start menu don't get the shell's PATH, so check the usual places too.
  const exe = process.platform === "win32" ? "ollama.exe" : "ollama";
  const known = {
    darwin: ["/Applications/Ollama.app/Contents/Resources/ollama", "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"],
    win32: [path.join(process.env.LOCALAPPDATA || "", "Programs", "Ollama", "ollama.exe")],
    linux: ["/usr/local/bin/ollama", "/usr/bin/ollama"],
  }[process.platform] || [];
  const onPath = (process.env.PATH || "").split(path.delimiter).map((dir) => path.join(dir, exe));
  return [...known, ...onPath].find((candidate) => candidate && fs.existsSync(candidate)) || null;
}

async function ensureOllama() {
  if (await waitFor(`${OLLAMA_URL}/api/tags`, 500)) return "running";
  const exe = findOllama();
  if (!exe) return "missing";
  if (!ollama || ollama.exitCode !== null) {
    ollama = spawn(exe, ["serve"], { stdio: "ignore", windowsHide: true });
    ollama.on("error", () => { ollama = null; });
  }
  return (await waitFor(`${OLLAMA_URL}/api/tags`, 20000)) ? "started" : "failed";
}

function openExternal(url) {
  if (/^https:\/\//.test(url)) shell.openExternal(url);
}

function fromApp(event) {
  return base && event.senderFrame && event.senderFrame.url.startsWith(`${base}/`);
}

ipcMain.handle("pick-folder", async (event) => {
  if (!fromApp(event)) return null;
  const result = await dialog.showOpenDialog(mainWindow, { title: "Choose a folder of documents", properties: ["openDirectory"] });
  return result.canceled ? null : result.filePaths[0];
});
ipcMain.handle("start-model-server", (event) => (fromApp(event) ? ensureOllama() : null));
ipcMain.handle("open-ollama-download", (event) => { if (fromApp(event)) openExternal(OLLAMA_DOWNLOAD); });

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 900,
    minWidth: 720,
    minHeight: 560,
    title: "casefile",
    backgroundColor: "#FFFFFF",
    show: false,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => { mainWindow = null; });
  // Only the engine's own pages open in the window; links elsewhere go to the user's browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url === STARTING_URL) return; // the starting screen, and no other local file
    try {
      if (base && new URL(url).origin === base) return;
    } catch {
      /* not a URL we can parse: refuse it below */
    }
    event.preventDefault();
    openExternal(url);
  });
  mainWindow.loadFile(STARTING_PAGE);
}

app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.defaultSession.on("will-download", (_event, item) => {
    item.setSaveDialogOptions({ defaultPath: path.join(app.getPath("downloads"), item.getFilename()) });
  });
  createWindow();
  try {
    await Promise.all([startEngine(), ensureOllama()]);
    await session.defaultSession.cookies.set({ url: base, name: "casefile_token", value: TOKEN, httpOnly: true, sameSite: "strict" });
    if (mainWindow) await mainWindow.loadURL(`${base}/`);
  } catch (error) {
    dialog.showErrorBox("casefile couldn't start", error.message);
    app.quit();
  }
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  quitting = true;
  if (engine && engine.exitCode === null) {
    engine.stdin.end();
    engine.kill();
  }
  if (ollama && ollama.exitCode === null) ollama.kill();
});
