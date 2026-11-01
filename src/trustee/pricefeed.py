"""Verify a delivered competitor price feed against live market data from Channel3.

The feed is a CSV: ``retailer, product_url, product_title, price, currency, observed_at``. The buyer does not trust
it. It samples rows with a seed derived from the signed contract digest, looks each one up in Channel3 and marks a
row valid when the product exists, a listing at that retailer exists, the currency matches and the price is within
``max(5%, $1)`` of the market price. Plain Decimal arithmetic decides; no model is involved.

Sampling is reproducible: row ``i`` gets the sort key ``sha256("<contract digest>:<i>")`` and the first N keys win.
Seller and buyer both hold the contract, so either can recompute the exact sample (``sample_indices``).

The result plugs into the existing buyer hooks:

* as a *checker* it returns ``(ok, detail)``; a failure starts with ``failed``, the marker the arbiter keys its
  objective-breach rule on, and names the ``rows`` clause the contract signed;
* as a *unit counter* it returns ``(units_valid, units_total)`` over the sampled rows, which the Trustee turns into
  a pro-rata payout when the contract carries a ``partial`` term;
* ``annotate`` adds evidence strings and one tape row per sampled check.

If Channel3 itself cannot answer (bad key, no credits, outage) the result is ``unverified: ...``, not ``failed``:
the seller did not breach anything, so the arbiter's objective-breach rule is not triggered and no counts are
reported.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .channel3 import (
    Channel3Client,
    Channel3Error,
    Channel3NotFound,
    Channel3RequestError,
    PriceHistory,
    Product,
    normalize_domain,
)
from .protocol import Contract

log = logging.getLogger(__name__)

GOOD_ID = "competitor-price-feed-2026q4"
COLUMNS = ("retailer", "product_url", "product_title", "price", "currency", "observed_at")
SAMPLE_SIZE = 20
TOLERANCE_PCT = Decimal("0.05")
TOLERANCE_FLOOR = Decimal("1.00")
MAX_ROWS = 100_000  # a sanity bound on the feed; the sample is what costs credits, not the size

CRITERION = f"market check: {SAMPLE_SIZE} sampled rows priced within max(5%, $1) of live Channel3 offers"

# Reasons a sampled row can be invalid.
OK = "ok"
MALFORMED = "malformed_row"
NOT_FOUND = "product_not_found"
NO_OFFER = "no_offer_for_retailer"
CURRENCY = "currency_mismatch"
OUT_OF_TOLERANCE = "price_out_of_tolerance"


def tolerance(market: Decimal) -> Decimal:
    """Allowed absolute difference: 5% of the market price, but never less than one dollar."""
    return max(market * TOLERANCE_PCT, TOLERANCE_FLOOR)


def sample_indices(contract_digest: str, total: int, n: int = SAMPLE_SIZE) -> list[int]:
    """The row indices (0-based, ascending) both parties must check for this contract and feed length."""
    keyed = sorted(range(total), key=lambda i: hashlib.sha256(f"{contract_digest}:{i}".encode()).hexdigest())
    return sorted(keyed[: min(n, total)])


@dataclass(frozen=True)
class RowCheck:
    index: int  # 0-based position among the data rows
    retailer: str
    product: str
    feed_price: str
    market_price: str  # "" when there was no market price
    currency: str
    ok: bool
    reason: str

    def note(self) -> str:
        market = self.market_price or "n/a"
        verdict = "ok" if self.ok else self.reason
        return (
            f"row {self.index + 1}: feed {self.feed_price} vs market {market} {self.currency} ({verdict}), "
            f"{self.product[:60]} @ {self.retailer}"
        )


@dataclass
class FeedReport:
    source: str  # "live" | "recorded"
    rows_total: int = 0
    checks: list[RowCheck] = field(default_factory=list)
    schema_error: str = ""
    unverified: str = ""  # Channel3 could not answer; nothing was proven either way
    fair_price: str = ""
    credits: int = 0

    @property
    def units_total(self) -> int:
        return len(self.checks)

    @property
    def units_valid(self) -> int:
        return sum(1 for c in self.checks if c.ok)


def _clean(value: str) -> str:
    return value.strip()


def _parse_price(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def read_feed(artifact: Path) -> tuple[list[dict[str, str]] | None, str]:
    """(rows, error). A header or encoding problem returns ``(None, message)``; a bad row is kept and judged later."""
    try:
        text = artifact.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"not readable as UTF-8 text ({type(exc).__name__})"
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None or tuple(header) != COLUMNS:
        found = ",".join(header or [])[:120]
        return None, f"schema mismatch, header is {found or '(empty)'}"
    rows: list[dict[str, str]] = []
    for row in reader:
        if len(rows) >= MAX_ROWS:
            return None, f"more than {MAX_ROWS} rows"
        if len(row) != len(COLUMNS):
            rows.append({c: "" for c in COLUMNS})  # a short or long line is an invalid row, not a crash
            continue
        rows.append(dict(zip(COLUMNS, (_clean(v) for v in row), strict=True)))
    return rows, ""


def _market_product(client: Channel3Client, row: dict[str, str]) -> Product | None:
    """Resolve the row's product: by URL when it has one, else by title. None when Channel3 does not know it."""
    url = row["product_url"]
    if url:
        try:
            return client.lookup(url)
        except (Channel3NotFound, Channel3RequestError):
            return None
    retailer = normalize_domain(row["retailer"])
    for product in client.search(row["product_title"], limit=5):
        if any(o.domain == retailer for o in product.offers):
            return product
    return None


def check_row(client: Channel3Client, index: int, row: dict[str, str]) -> tuple[RowCheck, Product | None]:
    """Judge one row. Channel3Error escapes only for failures that say nothing about the seller (auth, outage)."""
    price = _parse_price(row["price"])
    currency = row["currency"].upper()
    retailer = normalize_domain(row["retailer"])

    def result(market: str, ok: bool, reason: str) -> RowCheck:
        label = row["product_title"] or row["product_url"]
        return RowCheck(index, row["retailer"], label, row["price"][:20], market, currency, ok, reason)

    if price is None or len(currency) != 3 or not retailer or not (row["product_url"] or row["product_title"]):
        return result("", False, MALFORMED), None
    product = _market_product(client, row)
    if product is None:
        return result("", False, NOT_FOUND), None
    offers = [o for o in product.offers if o.domain == retailer]
    if not offers:
        return result("", False, NO_OFFER), product
    offer = next((o for o in offers if o.in_stock), offers[0])
    if offer.currency != currency:
        return result(str(offer.price), False, CURRENCY), product
    if abs(price - offer.price) <= tolerance(offer.price):
        return result(str(offer.price), True, OK), product
    return result(str(offer.price), False, OUT_OF_TOLERANCE), product


def fair_price_note(history: PriceHistory) -> str:
    rng = ""
    if history.min_price is not None and history.max_price is not None:
        rng = f", range {history.min_price}-{history.max_price} {history.currency}"
    price = f"{history.current_price} {history.currency}" if history.current_price is not None else "n/a"
    return f"Channel3 history: current market price {price} is {history.current_status or 'unknown'}{rng}"


def verify_feed(
    artifact: Path,
    contract_digest: str,
    client: Channel3Client,
    *,
    sample_size: int = SAMPLE_SIZE,
    fair_price: bool = False,
) -> FeedReport:
    report = FeedReport(source=client.mode)
    rows, error = read_feed(artifact)
    if rows is None:
        report.schema_error = error
        return report
    report.rows_total = len(rows)
    calls_before = client.calls
    first_product: Product | None = None
    try:
        for i in sample_indices(contract_digest, len(rows), sample_size):
            check, product = check_row(client, i, rows[i])
            report.checks.append(check)
            if first_product is None and product is not None:
                first_product = product
        if fair_price and first_product is not None:
            try:
                report.fair_price = fair_price_note(client.price_history(first_product.id))
            except Channel3NotFound:
                pass  # the product is not tracked; the note is optional
    except Channel3Error as exc:
        report.unverified = f"Channel3 could not be reached for a sampled row ({exc})"
        report.checks = []
    report.credits = client.calls - calls_before if client.mode == "live" else 0
    return report


class PriceFeedVerifier:
    """Callable checker + counter + annotator for ``competitor-price-feed-2026q4``.

    ``needs_contract`` tells the buyer to pass the contract (its digest seeds the sample). One verification per
    (file bytes, contract digest) is cached so the checker, the counter and the tape rows share a single pass over
    Channel3: a run spends its credits once.
    """

    needs_contract = True

    def __init__(
        self,
        client_factory: Callable[[], Channel3Client] | None = None,
        *,
        sample_size: int = SAMPLE_SIZE,
        fair_price: bool | None = None,
    ):
        self._factory = client_factory or Channel3Client.from_env
        self.sample_size = sample_size
        self._fair_price = fair_price
        self._cache: dict[tuple[str, str], FeedReport] = {}
        self.counter = _Units(self)

    def report(self, artifact: Path, contract: Contract | None) -> FeedReport:
        if contract is None:
            raise ValueError("the price-feed check needs the contract: its digest seeds the sample")
        try:
            sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError:
            return FeedReport(source="", schema_error=f"artifact missing: {artifact.name}")
        key = (sha, contract.digest())
        if key not in self._cache:
            if len(self._cache) >= 64:
                self._cache.clear()
            client = self._factory()
            try:
                self._cache[key] = verify_feed(
                    artifact, key[1], client, sample_size=self.sample_size, fair_price=self._want_fair_price()
                )
            finally:
                client.close()
        return self._cache[key]

    def _want_fair_price(self) -> bool:
        if self._fair_price is not None:
            return self._fair_price
        return os.environ.get("CHANNEL3_FAIR_PRICE") == "1"

    def __call__(self, artifact: Path, good_id: str, contract: Contract | None = None) -> tuple[bool, str]:
        report = self.report(artifact, contract)
        if report.schema_error:
            return False, f"failed: {report.schema_error}"
        if report.unverified:
            return False, f"unverified: {report.unverified}"
        if report.units_total == 0:
            return False, "failed: no rows to check in the feed"
        bad = report.units_total - report.units_valid
        if bad:
            return False, (
                f"failed: market check: {bad} of {report.units_total} sampled rows not within tolerance of "
                f"{report.source} Channel3 offers"
            )
        return True, (
            f"market check ok: {report.units_valid} of {report.units_total} sampled rows within tolerance of "
            f"{report.source} Channel3 offers ({report.rows_total} feed rows)"
        )

    def annotate(self, artifact: Path, contract: Contract) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """(extra evidence strings, ledger payloads for the tape). Reuses the cached pass."""
        report = self.report(artifact, contract)
        evidence = {
            "market_source": report.source,
            "market_rows_sampled": str(report.units_total),
            "market_rows_valid": str(report.units_valid),
            "market_tolerance": "max(5%, 1.00)",
        }
        if report.fair_price:
            evidence["market_fair_price"] = report.fair_price[:200]
        payloads: list[dict[str, Any]] = [
            {
                "event": "market_check",
                "rule": "market_price",
                "ok": c.ok,
                "note": c.note(),
            }
            for c in report.checks
        ]
        if report.fair_price:
            payloads.append({"event": "fair_price", "note": report.fair_price})
        return evidence, payloads


class _Units:
    """The unit counter over the same cached pass: (valid sampled rows, sampled rows)."""

    needs_contract = True

    def __init__(self, verifier: PriceFeedVerifier):
        self._verifier = verifier

    def __call__(self, artifact: Path, good_id: str, contract: Contract | None = None) -> tuple[int, int]:
        report = self._verifier.report(artifact, contract)
        if report.schema_error or report.unverified:
            return 0, 0
        return report.units_valid, report.units_total


_DEFAULT: PriceFeedVerifier | None = None


def default_verifier() -> PriceFeedVerifier:
    """The process-wide verifier the arena and the A2A agents register for ``GOOD_ID``."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PriceFeedVerifier()
    return _DEFAULT
