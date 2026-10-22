# Changelog

All notable changes to casefile. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). The IDs in brackets refer to the
review findings fixed (S = security, C = correctness, I = other improvements).

## [0.2.0] - Unreleased

### Security

- Deleting a report now also deletes the model's cached answers for its documents, which hold
  names and quotes. New `casefile clear-cache` command and a "delete all data" API
  (`DELETE /api/data`) remove everything. [S1]
- Uploaded copies of documents are deleted as soon as a job fails, is cancelled or is found
  half-uploaded after a restart, not only when it succeeds; failed and unfinished jobs can be
  deleted from the web page. [S2, C10]
- An event now only counts as verified if its quote matches at least 5 words and 25 characters
  of the source, with every `...`-separated piece at least 3 words, so a one-word quote can no
  longer pass. [S3]
- Document text is fenced in the prompt by a marker the document can't fake, text hidden with
  CSS is not sent to the model, and two full names are only merged into one person when the
  document itself links them. [S4]
- `casefile serve` now needs a secret for every request: it prints a one-time link that sets an
  HttpOnly cookie, so other users and programs on the same computer can't read reports. [S5]
- The desktop app sets Electron's security fuses (no `ELECTRON_RUN_AS_NODE`, no Node options or
  inspector flags, asar integrity checked) and only navigates to its own start page and
  engine. [S6] Cookie encryption is deliberately left off: its only cookie is a secret that
  changes every launch, and on macOS it put a Keychain permission prompt in front of start-up
  after every update of the unsigned app.
- The app and the engine talk to the same model server, `127.0.0.1:11434`, so another local user
  can't intercept documents on `localhost`/`::1`. [S7]
- The engine picks its own port and reports it to the app, closing a race in which another
  program could grab the port and the app's secret. [S8]
- Release builds are reproducible and verifiable: every GitHub Action is pinned to a commit,
  workflows have read-only permissions by default, the engine is built from a hashed lock file
  with a pinned PyInstaller, and each installer gets a signed build-provenance attestation and a
  SHA-256 checksum. [S9]
- The background worker can no longer die silently and leave later jobs queued forever. [S10]

### Fixed

- Password-protected PDFs that open without a password (AES "edit-locked" PDFs from banks and
  agencies) are read instead of failing. [C1]
- One bad part (an over-long prompt, a model crash, odd JSON) is recorded and skipped instead of
  stopping the whole run; only an unreachable server or a missing model stops it. A dropped
  connection says to run again to continue. [C2, C11, C18]
- People: "Dr. Patel" with the alias "Patel" no longer becomes "Patel Patel"; a first name used
  as an actor joins the matching person named in the same part; "Sarah Kim, CFO" no longer reads
  CFO as a surname. [C3, C4, C17]
- Very long quotes or summaries no longer make the PDF fail after the run has finished. [C5]
- Cyrillic and Chinese/Japanese/Korean names and quotes print in the PDF instead of blank boxes. [C6]
- Parts in Chinese, Japanese, Korean or Thai are sized by estimated tokens so they fit the
  model's context window. [C7]
- UTF-16 text files (Windows "Unicode" exports) are read correctly. [C8]
- `casefile report` names the PDF after the whole folder name, so "Smith v. Jones" gives
  "Smith v. Jones.pdf". [C9]
- A desktop job whose folder has gone fails with a plain message instead of an empty
  report. [C12]
- HTML table cells are kept apart; RTF files are read as text, not markup. [C13, C14]
- After a restart, queued jobs run in the order they were created. [C15]
- More date wordings are understood: "Sept 2023", "April 5 2023", "5 April, 2023",
  "Monday, 5 April 2023", and month names in other languages. [C16, I8]
- Web page: a failed upload offers to choose the folder again or delete the job, and a slow
  status check no longer pulls you back to a page you have left. [C19]
- People are no longer split by initials or short forms ("S" for Sarah, "Jas" for Jaswinder,
  "P. Doyle" labelled "P"); professional letters such as MRICS are not read as a surname; and
  organisations ("Hollis & Sons Ltd") are not listed as people. Found by the new evaluation:
  correct name labels went from 56% to 88%.

### Changed (web page and app)

- The activity log reads as a list of what happened: the file, line and quote behind each event
  moved into a small grey *Show source* under its date.
- Large reports stay fast: months are built as you scroll to them, long lists (key events, files,
  people, entries to check) show a page at a time, the file and people lists can be searched,
  files that couldn't be read can be listed on their own, and long folder paths are shortened.

### Added

- Attachments inside emails are read and listed in the report's sources. [I1]
- New formats: Outlook `.msg`, mailbox `.mbox`, Excel `.xlsx`, PowerPoint `.pptx`,
  OpenDocument `.odt`, and old Word `.doc` where the system can convert it. [I2]
- Running reports can be cancelled. [I3]
- Continuous integration on every push and pull request: the tests on Linux, macOS and Windows,
  the web page in a real browser, and a syntax check of the desktop app's scripts. [I4]
- An evaluation set of labelled example cases and `python -m evals.run`, which scores event
  recall and precision, importance agreement and people against a local model. [I5]
- `CONTRIBUTING.md`, `SECURITY.md`, this changelog, and issue and pull request templates. The
  version is now written once, in `casefile/__init__.py`. [I6]
- The desktop app's engine log is rotated at start-up once it passes 5 MB. [I7]
- Scanned PDFs are read with OCR when Tesseract is installed. [I9]

### Changed

- The default model server address is `http://127.0.0.1:11434` instead of
  `http://localhost:11434`. [S7]
- The macOS Apple-silicon installer is built on the `macos-15` runner (`macos-14` is being
  retired).
- `casefile serve` prints a link with a one-time secret; open that link instead of a bare
  address. [S5]
- The Docker image includes Tesseract and builds with the licence files the package now
  declares; open the link from `docker logs`.
- The macOS install note no longer suggests removing the quarantine flag; it points to
  *System Settings > Privacy & Security > Open Anyway*. [S6]

## [0.1.0] - 2026-10-22

### Added

- First release: turns a folder of documents (PDF, Word, email, HTML, text) into a prioritised,
  quote-checked log of events and a who's-who, using a local AI model. Command line
  (`casefile report`), browser page (`casefile serve`) and a desktop app for macOS, Windows and
  Linux.

[0.2.0]: https://github.com/RossShelford/casefile/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RossShelford/casefile/releases/tag/v0.1.0
