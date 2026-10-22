## What and why

<!-- What does this change, and what problem does it fix? Link the issue: "Fixes #123". -->

## How it was tested

<!-- New or changed tests; anything checked by hand (desktop app, web page, a real model run). -->

## Checklist

- [ ] Tests added or updated, and `pytest -q` passes (no real model or network in tests).
- [ ] If the prompt, schema or importance rubric changed: `PROMPT_VERSION` bumped, and the
      before/after "overall" output of `python -m evals.run` pasted below.
- [ ] If `dependencies` changed: `desktop/engine-requirements.txt` regenerated (see CONTRIBUTING.md).
- [ ] User-facing change: README and the "Unreleased" section of CHANGELOG.md updated.
- [ ] No real case documents or personal data anywhere in the change.

<!-- Eval results, if any:

before:

after:

-->
