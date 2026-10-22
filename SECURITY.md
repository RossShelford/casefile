# Security policy

casefile is used on private documents: tenancy disputes, employment cases, medical and legal
letters. Please report security problems privately so they can be fixed before they are public.

## Reporting a vulnerability

Use GitHub's private reporting: the repository's **Security** tab > **Report a vulnerability**
(<https://github.com/RossShelford/casefile/security/advisories/new>). Please don't open a public
issue, pull request or discussion for a security problem.

Include what you can of:

- the version (the release, or the commit) and how you run it: desktop app, `casefile serve` or
  `casefile report`, and the operating system;
- the steps or a proof of concept, and what an attacker gains;
- any document or request needed to reproduce it. Use made-up content, never real case files.

casefile is maintained by one person in spare time. I aim to acknowledge a report within a week,
agree a fix and a disclosure date with you, and credit you in the advisory and the
[changelog](CHANGELOG.md) unless you'd rather not be named.

## Supported versions

Only the latest release gets security fixes. Upgrade before reporting if you can.

## How casefile is meant to work

It is local-only by design:

- Documents are read on your computer and sent only to the model server you choose, by default
  Ollama at `http://127.0.0.1:11434`. casefile has no telemetry and makes no other network requests.
- `casefile serve` listens on `127.0.0.1` only, unless you pass `--host`. Every request needs a
  random secret created at start-up: the browser gets it from the one-time link the command
  prints, and keeps it in an HttpOnly, SameSite=Strict cookie. Requests that change something
  also need an `X-Casefile: 1` header, and the `Host` header must be an allowed name, which blocks
  other websites and DNS rebinding.
- The desktop app runs the same engine on a free local port with its own secret, which only the
  app's window holds.
- What is kept on disk: each report (until you delete it), and the model's answer for each part of
  each document, so an interrupted run can resume. Deleting a report also deletes its cached
  parts and any uploaded copies; `casefile clear-cache`, or `DELETE /api/data` on the engine's
  API, removes everything. Locations: `~/.cache/casefile/` for the command line and
  `casefile serve`, and the app's data folder for the desktop app.

## In scope

- Reading another user's reports or documents, or using the engine's API, without the secret.
- A web page in your browser making casefile do something (CSRF, DNS rebinding, XSS in the web
  page or the app window).
- A crafted document that makes casefile read or write files outside its own folders, run code,
  or exhaust memory or disk out of proportion to its size (zip bombs, XML entity tricks, etc.).
- Escaping the Electron sandbox, abusing the app's IPC bridge, or loading other content into the
  app window.
- Private content left on disk after casefile says it was deleted.
- Tampering with release builds: the workflow, the pinned dependencies, the installers.
- Document text that steers the model into producing a misleading report in ways the quote check
  should catch but doesn't (for example events marked as verified that the source doesn't support).

## Out of scope

- The model server itself (Ollama, LM Studio, llama.cpp, vLLM) and the model's general accuracy.
  Missed or misjudged events are bugs: please open a normal issue, ideally with an eval case.
- Running `casefile serve --host 0.0.0.0` (or behind a proxy) on a network you don't trust. That
  exposes your documents to that network; it's supported only on a network you control.
- Attacks that need administrator access, or the same user account, on the computer.
- macOS Gatekeeper and Windows SmartScreen warnings for unsigned installers.

## Verifying a release

Each installer on a release has a signed build-provenance attestation, and the release lists
SHA-256 checksums in `SHA256SUMS.txt`:

```sh
gh attestation verify <installer file> -R RossShelford/casefile
shasum -a 256 -c SHA256SUMS.txt --ignore-missing
```
