# Contributing to casefile

Thanks for helping. Bug reports, fixes, new file formats and better prompts are all welcome.
For anything large, open an issue first so we can agree the approach before you spend the time.

Security problems: please don't open a public issue; see [SECURITY.md](SECURITY.md).

## Set up

You need Python 3.10 or newer. For real runs you also need a local model server
(see the README); the tests never call a model.

```sh
git clone https://github.com/RossShelford/casefile.git && cd casefile
python -m venv .venv
.venv/bin/pip install -e '.[test,e2e]'
.venv/bin/playwright install chromium   # only for the browser tests
```

Optional: install [Tesseract](https://github.com/tesseract-ocr/tesseract) (`brew install tesseract`,
`apt install tesseract-ocr`) to run the scanned-PDF (OCR) tests; without it they are skipped.

## Run the tests

```sh
.venv/bin/pytest -q                                  # everything, incl. the eval scoring checks
.venv/bin/pytest -q --ignore=tests/test_web_e2e.py   # skip the browser tests
.venv/bin/pytest -q tests/test_web_e2e.py            # just the web page, in headless Chromium
```

CI (`.github/workflows/ci.yml`) runs the same on Linux, macOS and Windows for every push and pull
request, plus `node --check` on the Electron scripts. A pull request needs it green.

Tests must be deterministic and offline: no real model and no network, except fake servers
started by the test on `127.0.0.1`. Add a regression test with every behaviour change.

## Run and build the desktop app

```sh
cd desktop
npm install
npm start          # run from source, using the Python in ../.venv
npm run dist       # build the engine, then the installer for this computer, into desktop/release
```

`npm run build:engine` (part of `npm run dist`) installs the engine's Python packages from the
hashed lock `desktop/engine-requirements.txt` into the Python it uses (`$CASEFILE_PYTHON`, else
`.venv`), then casefile itself as an editable install, then freezes it with PyInstaller. So
building the app moves your `.venv` to the locked versions; rerun
`.venv/bin/pip install -e '.[test,e2e]'` afterwards if you need the test extras again.

Each operating system builds its own installer. Pushing a `v*` tag runs
`.github/workflows/desktop.yml`: CI, then every installer, a signed build-provenance attestation
for each, and a **draft** release with the installers and `SHA256SUMS.txt`. Check it, then publish
it by hand.

## Regenerate the engine lock

`desktop/engine-requirements.txt` pins every package that goes into the desktop engine, with
hashes, so a release can't pick up whatever PyPI serves on the day. Regenerate it whenever you
change `dependencies` in `pyproject.toml` or move to a new PyInstaller, and commit it with that
change:

```sh
printf 'pyinstaller==6.22.3\nsetuptools>=77\n' | .venv/bin/uv pip compile pyproject.toml - \
  --universal --generate-hashes --python-version 3.12 -o desktop/engine-requirements.txt
```

(`pip install uv` if your venv lacks it.) Put the PyInstaller version you want in the `printf`.
`setuptools` is there because casefile is installed without build isolation, so its build backend
must come from the lock too. `--universal` makes one file that works on macOS, Windows and Linux.
Add `--upgrade` to move every package to its newest allowed version.

## Changing the prompt or the model output schema

The extraction prompt (`casefile/extract.py`, plus the importance rubric in
`casefile/taxonomy.py`) decides what lands in every report, so changes need evidence:

1. Bump `PROMPT_VERSION` in `casefile/extract.py`. It is part of every cache key, so old cached
   answers are not reused with the new prompt.
2. Run the evaluation set before and after your change, with the same model:

   ```sh
   .venv/bin/python -m evals.run --model qwen2.5:7b                 # Ollama on 127.0.0.1:11434
   .venv/bin/python -m evals.run --model qwen2.5:7b --url http://127.0.0.1:11434 --json after.json
   ```

3. Put both "overall" blocks in the pull request. A prompt change should not lower event recall or
   precision, or importance agreement, without a good reason stated there.

## The evaluation set

`evals/cases/<case>/` holds made-up but realistic case folders in mixed formats (email with
attachments, Word, PDF, Excel, RTF, HTML, CSV, WhatsApp and Slack exports, notes). `evals/gold/<case>.json`
labels every event in them (date, importance, key words) and every person (acceptable labels).
`evals/run.py` runs the real pipeline against a local model and prints, per case and overall:

- **event recall and precision**: report events are matched one-to-one to gold events by date
  (equal, or one a less precise form of the other) and key words (at least half of them in the
  event's summary or quote); see `evals/score.py`;
- **importance agreement**: exact, and within one level;
- **people**: found, shown under an acceptable label, split into several, merged together.

To add a case: add the folder and its gold file with the same name. The `.docx`, `.pdf` and
`.xlsx` files are generated from text in `evals/make_fixtures.py`
(`.venv/bin/python -m evals.make_fixtures`). `evals/test_score.py` checks every gold file is
well-formed and that each gold event's key words actually occur in the case's documents.

All names, addresses, companies and reference numbers in the cases are invented. Never add real
documents.

## Versions and releases

The version is written once, in `casefile/__init__.py` (`__version__`); `pyproject.toml` reads it.
`desktop/package.json` must carry the same number (`tests/test_version.py` fails otherwise).
To release: bump both, move the "Unreleased" notes in `CHANGELOG.md` under the new version,
merge, then push a tag `vX.Y.Z`.

## Workflows and actions

Every action in `.github/workflows` is pinned to a full commit SHA with the version in a comment.
To update one, look up the commit for the new tag and change both together:

```sh
gh api repos/actions/checkout/git/ref/tags/v7.0.1 --jq .object
# if "type" is "tag" (an annotated tag), follow its URL once more to get the commit SHA
```

Keep the top-level `permissions: contents: read`; grant more only to the job that needs it.

## Style

- User-facing messages are plain English and say what to do next.
- Comments explain why, not what.
- Keep dependencies few; a new runtime dependency also means regenerating the engine lock.

## Licence

casefile is Apache-2.0. By contributing you agree that your contribution is licensed under the
same terms (section 5 of the [licence](LICENSE)).
