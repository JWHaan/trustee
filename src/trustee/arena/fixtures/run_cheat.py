"""Cheat-run entry point. `run_honest.main` is scenario-agnostic.

python -m trustee.arena.fixtures.run_cheat
python -m trustee.arena.fixtures.run_cheat --mode scripted --json
"""

from __future__ import annotations

import sys

from .run_honest import main

if __name__ == "__main__":
    raise SystemExit(main(["cheat", *sys.argv[1:]]))
