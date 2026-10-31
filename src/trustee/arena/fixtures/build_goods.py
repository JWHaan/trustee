"""Build the demo goods deterministically.

The story: Northwind Outfitters' demand-planning agent buys a Q3 2026 returns
dataset from Tidewater Data Co.'s sales agent. The buyer uses it to size the
refund reserve it holds back next quarter.

Deterministic matters: the manifest pins a sha256, so a file regenerated with
a different random seed, row order, or line ending would produce a different
hash on every machine and the honest run would fail its own acceptance
criterion. A seeded generator, a fixed row order, `\\n` line endings and UTF-8
keep the bytes identical everywhere.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
GOODS = ROOT / "goods"

SEED = 2026
HONEST_NAME = "returns-2026q3.csv"
JUNK_NAME = "returns-2026q3-junk.csv"
HONEST_ROWS = 5000
JUNK_ROWS = 312

COLUMNS = ["order_id", "sku", "category", "order_date", "return_date", "reason_code", "refund_usd"]
# The corrupt twin renames one column, the kind of silent schema drift a seller
# ships when its export job changes.
JUNK_COLUMNS = ["order_id", "sku", "category", "order_date", "return_date", "reason_code", "refund_amount"]

# category -> (sku prefixes, refund range in cents)
CATALOG: dict[str, tuple[list[str], tuple[int, int]]] = {
    "tops": (["TEE-CRW", "TEE-VNK", "HDY-ZIP", "SHR-OXF"], (1800, 6400)),
    "bottoms": (["JNS-SLM", "CHO-REG", "SHT-LIN"], (3500, 9800)),
    "outerwear": (["JKT-BMR", "PKR-WTR"], (8900, 24900)),
    "footwear": (["SNK-LOW", "BTS-CHL"], (5900, 15900)),
    "accessories": (["BLT-LTH", "CAP-WOL"], (1900, 7900)),
}
COLORS = ["BLK", "NVY", "GRY", "OLV", "WHT"]
SIZES = ["XS", "S", "M", "L", "XL"]
REASONS = ["SIZE_TOO_SMALL", "SIZE_TOO_LARGE", "DAMAGED", "NOT_AS_DESCRIBED", "CHANGED_MIND"]
ORDER_START = date(2026, 7, 1)
ORDER_SPAN_DAYS = 92  # 1 Jul to 30 Sep 2026


def _rows(rng: random.Random, count: int) -> list[list[str]]:
    rows: list[list[str]] = []
    categories = sorted(CATALOG)
    for i in range(count):
        category = rng.choice(categories)
        prefixes, (lo, hi) = CATALOG[category]
        order_day = ORDER_START + timedelta(days=rng.randrange(ORDER_SPAN_DAYS))
        return_day = order_day + timedelta(days=rng.randrange(0, 31))
        cents = rng.randrange(lo, hi + 1)
        rows.append(
            [
                f"NW-2026-{i + 1:06d}",
                f"{rng.choice(prefixes)}-{rng.choice(COLORS)}-{rng.choice(SIZES)}",
                category,
                order_day.isoformat(),
                return_day.isoformat(),
                rng.choice(REASONS),
                f"{cents // 100}.{cents % 100:02d}",
            ]
        )
    return rows


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def honest_bytes() -> bytes:
    return _csv_bytes(COLUMNS, _rows(random.Random(SEED), HONEST_ROWS))


def junk_bytes() -> bytes:
    rows = _rows(random.Random(SEED), JUNK_ROWS)
    # Duplicate order ids on every tenth row: the corrupt export reuses ids.
    for k in range(10, JUNK_ROWS, 10):
        rows[k][0] = rows[k - 1][0]
    return _csv_bytes(JUNK_COLUMNS, rows)


def main() -> None:
    GOODS.mkdir(parents=True, exist_ok=True)
    honest = honest_bytes()
    (GOODS / HONEST_NAME).write_bytes(honest)
    (GOODS / JUNK_NAME).write_bytes(junk_bytes())
    digest = hashlib.sha256(honest).hexdigest()
    manifest = {HONEST_NAME: digest}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{HONEST_NAME}  sha256={digest}  ({len(honest)} bytes, {HONEST_ROWS} rows)")
    print(f"{JUNK_NAME}  (corrupt twin, deliberately not in the manifest)")


if __name__ == "__main__":
    main()
