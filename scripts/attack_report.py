"""Run the attack set (tests/test_attacks.py) and print its results as a Markdown table.

    python scripts/attack_report.py            # print the table
    python scripts/attack_report.py --readme   # also rewrite the table in README.md between its markers

The rows come from the same functions CI runs as tests, so the published table cannot drift from what the code does.
Dry-run: no PayPal or LLM credentials needed.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START, END = "<!-- attack-table:start -->", "<!-- attack-table:end -->"


def table() -> tuple[str, int, int]:
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "src"))
    from test_attacks import CASES  # noqa: PLC0415 - the test module is the source of truth

    lines = ["| Attack | Trustee's defence | Result |", "|---|---|---|"]
    passed = 0
    for case in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                observed = case.run(Path(tmp))
                mark = f"✅ {observed}"
                passed += 1
            except Exception as exc:  # noqa: BLE001 - a failed defence is a row, not a crash
                mark = f"❌ {type(exc).__name__}: {str(exc)[:80]}"
        lines.append(f"| {case.attack} | {case.defence} | {mark} |")
    stamp = time.strftime("%Y-%m-%d")
    lines.append("")
    lines.append(f"{passed} of {len(CASES)} defences held ({stamp}, dry-run, `python scripts/attack_report.py`).")
    return "\n".join(lines), passed, len(CASES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--readme", action="store_true", help="rewrite the table in README.md between its markers")
    args = parser.parse_args(argv)
    text, passed, total = table()
    print(text)
    if args.readme:
        readme = ROOT / "README.md"
        body = readme.read_text()
        if START not in body or END not in body:
            print("README.md has no attack-table markers", file=sys.stderr)
            return 2
        head, rest = body.split(START, 1)
        _, tail = rest.split(END, 1)
        readme.write_text(f"{head}{START}\n{text}\n{END}{tail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
