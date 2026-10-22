"""Entry point frozen by PyInstaller into the app's engine (see scripts/build-engine.mjs)."""

import sys

from casefile.cli import main

if __name__ == "__main__":
    sys.exit(main())
