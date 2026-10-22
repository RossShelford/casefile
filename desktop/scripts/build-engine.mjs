// Freeze the Python engine into ../dist/casefile-engine with PyInstaller, for the current OS.
// Uses $CASEFILE_PYTHON, else the repo's .venv, else `python` on PATH (CI).
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(desktop, "..");
const venv = process.platform === "win32" ? join(repo, ".venv", "Scripts", "python.exe") : join(repo, ".venv", "bin", "python");
const python = process.env.CASEFILE_PYTHON || (existsSync(venv) ? venv : "python");

const run = (args) => execFileSync(python, args, { cwd: repo, stdio: "inherit" });

// Every dependency, PyInstaller and the build backend come from a lock with pinned versions and
// hashes, so a release can't pick up whatever PyPI serves on the day (see CONTRIBUTING.md to
// regenerate it). casefile itself goes in last, editable and without dependencies or build
// isolation, so nothing unhashed is fetched and a developer's working copy is left in place.
run(["-m", "pip", "install", "--quiet", "--require-hashes", "-r", join(desktop, "engine-requirements.txt")]);
run(["-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation", "-e", "."]);
run([
  "-m", "PyInstaller",
  "--noconfirm", "--clean", "--onedir",
  "--name", "casefile-engine",
  "--distpath", join(repo, "dist"),
  "--workpath", join(repo, "build", "pyinstaller"),
  "--specpath", join(repo, "build"),
  // uvicorn and reportlab load parts of themselves by name at runtime.
  "--collect-submodules", "uvicorn",
  "--collect-submodules", "reportlab",
  "--collect-data", "reportlab",
  // Fonts, icons and the web page ship inside the casefile package.
  "--collect-data", "casefile",
  join(desktop, "engine_entry.py"),
]);
console.log(`engine built: ${join(repo, "dist", "casefile-engine")}`);
