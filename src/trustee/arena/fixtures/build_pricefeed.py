"""Build the competitor price-feed demo goods and the recorded Channel3 market snapshot, deterministically.

    uv run python -m trustee.arena.fixtures.build_pricefeed

The recorded snapshot is SYNTHETIC. It follows the response shapes in Channel3's API reference (retailer domains
end in ``.example``), so the scenario and the tests run with no key and no network; it was not captured from the live
API. With ``CHANNEL3_API_KEY`` set the same feeds are checked against real catalog data instead, and the synthetic
products will not be found there (use a feed of real product URLs for a live run, see docs/CHANNEL3.md).

Three feeds of 40 rows over the same 40 products:

* ``pricefeed-2026q4.csv``        honest: every price within ~2% of the market snapshot.
* ``pricefeed-2026q4-stale.csv``  stale and partly invented: prices 28-40% off, 6 products that do not exist.
* ``pricefeed-2026q4-mixed.csv``  8 bad rows (4 prices 35% off, 4 invented products), 32 good: a pro-rata case. With
                                  a sample of 20, at most 8 sampled rows are bad, so at least 60% are valid.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
GOODS = ROOT / "goods"
CHANNEL3 = ROOT / "channel3"

SEED = 20261031
COLUMNS = ["retailer", "product_url", "product_title", "price", "currency", "observed_at"]
HONEST_NAME = "pricefeed-2026q4.csv"
STALE_NAME = "pricefeed-2026q4-stale.csv"
MIXED_NAME = "pricefeed-2026q4-mixed.csv"
PRODUCTS = 40

RETAILERS = ["alpinegear.example", "trailhead-outfitters.example", "summitsupply.example", "basecampgoods.example"]
BRANDS = ["Ridgeline", "Northfork", "Kestrel", "Marlow", "Tamarack"]
ITEMS = [
    "Insulated Water Bottle 32oz",
    "Trail Daypack 24L",
    "Merino Base Layer Top",
    "Packable Rain Shell",
    "Trekking Poles Pair",
    "Headlamp 400 Lumen",
    "Camp Stove Compact",
    "Down Sleeping Bag 20F",
]


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _catalog(rng: random.Random) -> list[dict[str, Any]]:
    """40 products, each with a primary retailer offer and a second retailer's offer."""
    products: list[dict[str, Any]] = []
    for i in range(PRODUCTS):
        brand = BRANDS[i % len(BRANDS)]
        item = ITEMS[(i // len(BRANDS)) % len(ITEMS)]
        title = f"{brand} {item} Model {i + 1:02d}"
        primary = RETAILERS[i % len(RETAILERS)]
        secondary = RETAILERS[(i + 1) % len(RETAILERS)]
        base = Decimal(rng.randrange(1800, 38000)) / 100
        slug = title.lower().replace(" ", "-")
        products.append(
            {
                "id": f"c3-{i + 1:03d}",
                "title": title,
                "primary": primary,
                "price": _money(base),
                "url": f"https://{primary}/products/{slug}",
                "second": secondary,
                "second_price": _money(base * Decimal(str(round(rng.uniform(0.96, 1.08), 3)))),
                "second_url": f"https://{secondary}/p/{slug}",
            }
        )
    return products


def _product_json(p: dict[str, Any]) -> dict[str, Any]:
    def offer(domain: str, url: str, price: Decimal) -> dict[str, Any]:
        return {
            "url": url,
            "domain": domain,
            "price": {"price": float(price), "compare_at_price": None, "currency": "USD"},
            "availability": "InStock",
            "condition": "new",
        }

    return {
        "id": p["id"],
        "title": p["title"],
        "brands": [{"id": "b-" + p["title"].split()[0].lower(), "name": p["title"].split()[0]}],
        "offers": [offer(p["primary"], p["url"], p["price"]), offer(p["second"], p["second_url"], p["second_price"])],
    }


def _history_json(p: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    current = p["price"]
    mean = _money(current * Decimal(str(round(rng.uniform(0.93, 1.07), 3))))
    status = "low" if current < mean * Decimal("0.95") else "high" if current > mean * Decimal("1.05") else "typical"
    return {
        "canonical_product_id": p["id"],
        "product_title": p["title"],
        "history": [
            {"timestamp": "2026-10-01T00:00:00Z", "price": float(mean), "currency": "USD"},
            {"timestamp": "2026-10-15T00:00:00Z", "price": float(_money(mean * Decimal("1.01"))), "currency": "USD"},
            {"timestamp": "2026-10-31T00:00:00Z", "price": float(current), "currency": "USD"},
        ],
        "statistics": {
            "mean": float(mean),
            "std_dev": float(_money(mean * Decimal("0.04"))),
            "min_price": float(_money(min(current, mean) * Decimal("0.97"))),
            "max_price": float(_money(max(current, mean) * Decimal("1.03"))),
            "current_price": float(current),
            "current_status": status,
            "currency": "USD",
        },
    }


def _feed_row(p: dict[str, Any], index: int, price: Decimal, observed: str, *, invented: bool) -> list[str]:
    title = p["title"]
    url = p["url"]
    if invented:
        title = f"{title} Pro Edition"
        url = url.replace("/products/", "/products/pro-edition-")
    title_only = index % 5 == 0
    return [p["primary"], "" if title_only else url, title, f"{price}", "USD", observed]


def _csv_bytes(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _jitter(rng: random.Random, price: Decimal) -> Decimal:
    return _money(price * Decimal(str(round(rng.uniform(0.985, 1.015), 4))))


def honest_bytes(products: list[dict[str, Any]]) -> bytes:
    rng = random.Random(SEED + 1)
    rows = [_feed_row(p, i, _jitter(rng, p["price"]), "2026-10-31", invented=False) for i, p in enumerate(products)]
    return _csv_bytes(rows)


def stale_bytes(products: list[dict[str, Any]]) -> bytes:
    """Last spring's scrape: prices have moved 28-40% since, and some listed products never existed."""
    rng = random.Random(SEED + 2)
    rows = []
    for i, p in enumerate(products):
        invented = i % 7 == 0
        low, high = (1.28, 1.40) if i % 2 == 0 else (0.60, 0.72)
        factor = Decimal(str(round(rng.uniform(low, high), 3)))
        rows.append(_feed_row(p, i, _money(p["price"] * factor), "2026-04-15", invented=invented))
    return _csv_bytes(rows)


def mixed_bytes(products: list[dict[str, Any]]) -> bytes:
    """32 good rows and 8 bad ones (every fifth row from the fifth: 4 priced 35% high, 4 invented)."""
    rng = random.Random(SEED + 3)
    rows = []
    for i, p in enumerate(products):
        if i % 5 == 4:
            invented = (i // 5) % 2 == 1
            price = p["price"] if invented else _money(p["price"] * Decimal("1.35"))
            rows.append(_feed_row(p, i, price, "2026-10-31", invented=invented))
        else:
            rows.append(_feed_row(p, i, _jitter(rng, p["price"]), "2026-10-31", invented=False))
    return _csv_bytes(rows)


def build() -> dict[str, bytes]:
    rng = random.Random(SEED)
    products = _catalog(rng)
    feeds = {
        HONEST_NAME: honest_bytes(products),
        STALE_NAME: stale_bytes(products),
        MIXED_NAME: mixed_bytes(products),
    }
    lookup: dict[str, Any] = {}
    search: dict[str, Any] = {}
    for p in products:
        pj = _product_json(p)
        lookup[p["url"]] = {"id": pj["id"], "title": pj["title"], "brands": pj["brands"], "offers": pj["offers"]}
        search[p["title"]] = [pj]
    history = {p["id"]: _history_json(p, rng) for p in products}
    for name, data in (("lookup.json", lookup), ("search.json", search), ("history.json", history)):
        feeds[f"channel3/{name}"] = (json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    return feeds


def main() -> None:
    GOODS.mkdir(parents=True, exist_ok=True)
    CHANNEL3.mkdir(parents=True, exist_ok=True)
    for name, payload in build().items():
        if name.startswith("channel3/"):
            (CHANNEL3 / name.split("/", 1)[1]).write_bytes(payload)
            print(f"{name}  ({len(payload)} bytes)")
        else:
            (GOODS / name).write_bytes(payload)
            print(f"{name}  sha256={hashlib.sha256(payload).hexdigest()}  ({payload.count(b'\n') - 1} rows)")


if __name__ == "__main__":
    main()
