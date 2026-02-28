#!/usr/bin/env python3
"""Run only phase 1 of OpenClaw -> Creel migration."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taskrunner.migrations.openclaw import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main(["--phases", "1", *sys.argv[1:]]))
