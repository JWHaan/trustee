"""The judge's no-UI fallback: print one full run to stdout and exit.

    python -m trustee.arena.fixtures.run_honest
    python -m trustee.arena.fixtures.run_cheat --json

Exit code is 0 only when the deal reached the expected terminal state, so this
doubles as the reliability check in CI and in the 10/10 drill.
"""

from __future__ import annotations

import argparse
import json
import sys

from ...ledger import Ledger
from ...paypal import PayPalClient
from ..rows import to_row
from ..runner import DealRunner
from ..scenario import get_scenario

EXPECTED = {"honest": "SETTLED", "cheat": "VOIDED", "overreach": "SETTLED"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one arena scenario headless.")
    parser.add_argument("scenario", nargs="?", default="honest", choices=sorted(EXPECTED))
    parser.add_argument("--mode", default="auto", choices=("auto", "live", "scripted"))
    parser.add_argument("--json", action="store_true", help="emit the raw event stream as JSONL")
    args = parser.parse_args(argv)

    rows: list[dict] = []
    ledger = Ledger()
    runner = DealRunner(ledger=ledger, paypal=PayPalClient())
    result = runner.run(
        get_scenario(args.scenario),
        mode=args.mode,
        on_event=lambda ev: rows.append(to_row(ev, len(rows))),
    )

    if args.json:
        for row in rows:
            print(json.dumps(row))
        print(json.dumps({"result": result.snapshot()}))
    else:
        print(f"\n  {result.scenario} · {result.effective_mode} · deal {result.deal_id}\n")
        print(f"  {'ts':<12} {'actor':<14} {'kind':<9} {'state':<22} {'price':>8}  note")
        print(f"  {'-' * 12} {'-' * 14} {'-' * 9} {'-' * 22} {'-' * 8}  {'-' * 40}")
        for row in rows:
            state = f"{row['state_from']}->{row['state_to']}" if row["state_from"] else ""
            print(
                f"  {row['ts'][11:23]:<12} {row['actor']:<14} {row['kind']:<9} {state:<22} "
                f"{row['price']:>8}  {row['note'][:60]}"
            )
        print()
        if result.ruling:
            print(f"  ruling: {result.ruling['decision']} on clause '{result.ruling['clause']}'")
        if result.error:
            print(f"  error:  {result.error}")
        print(f"\n  final state: {result.state}  ({len(rows)} events)\n")

    expected = EXPECTED[args.scenario]
    if result.state != expected:
        print(f"FAIL: expected {expected}, got {result.state}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
