"""A small Channel3 client: live market data for checking a delivered price feed.

Channel3 (https://channel3.ai) is a product catalog API. Trustee uses it as outside evidence: the buyer looks up a
sample of the rows a seller delivered and compares the seller's prices with what retailers list now.

Two modes, chosen from the environment:

* ``live``      calls ``CHANNEL3_BASE_URL`` (default https://api.trychannel3.com) with ``x-api-key: $CHANNEL3_API_KEY``.
* ``recorded``  serves JSON fixtures from ``arena/fixtures/channel3/``. No key, no network. This is the default
                whenever ``CHANNEL3_API_KEY`` is unset, and ``CHANNEL3_MODE=recorded`` forces it even with a key.

The client only reads. It has no checkout, no payment and no pay-per-call (MPP/Tempo) support, and it never
logs or repr()s the key. Prices are ``Decimal``; a float from the wire is converted through ``str`` so ``19.99``
stays ``19.99``.

Endpoints used (shapes from https://channel3.mintlify.dev/docs/api-reference):

* ``POST /v1/search``  body ``{"query", "limit"}``  ->  ``{"products": [Product]}``       (1 credit)
* ``POST /v1/lookup``  body ``{"url"}``             ->  ``{"product": Product}``          (1 credit)
* ``GET  /v0/price-tracking/history/{id}?days=N``   ->  ``{"history": [...], "statistics": {...}}``
  (only for products you track; tracking is billed separately at 1 credit per product per day)

A Product carries ``offers[]``; each offer has ``domain``, ``url``, ``availability`` and ``price``. The OpenAPI
schema names the amount ``price.price``; the guide pages show ``price.amount``. Both are accepted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.trychannel3.com"
FIXTURES_DIR = Path(__file__).parent / "arena" / "fixtures" / "channel3"
DEFAULT_TIMEOUT = 10.0


class Channel3Error(Exception):
    """Any failure talking to Channel3. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Channel3AuthError(Channel3Error):
    """401: the key is missing or invalid."""


class Channel3CreditsError(Channel3Error):
    """402: the key is out of credits (or the API asks for pay-per-call, which Trustee never uses)."""


class Channel3RequestError(Channel3Error):
    """422: the request is not valid, e.g. the URL is not a product page."""


class Channel3NotFound(Channel3Error):
    """404: no such product."""


class Channel3Unavailable(Channel3Error):
    """Timeout, network failure, 5xx, or a response that is not the documented shape."""


@dataclass(frozen=True)
class Offer:
    domain: str
    url: str
    price: Decimal
    currency: str
    in_stock: bool


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    offers: tuple[Offer, ...] = ()


@dataclass(frozen=True)
class PriceHistory:
    canonical_product_id: str
    current_price: Decimal | None
    current_status: str  # "low" | "typical" | "high" | ""
    mean: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    currency: str
    points: int


def normalize_domain(value: str) -> str:
    """``https://www.Shop.example/x`` -> ``shop.example``; a bare domain is lower-cased the same way."""
    text = value.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    return text[4:] if text.startswith("www.") else text


def _decimal(raw: Any, what: str) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise Channel3Unavailable(f"unexpected {what} in Channel3 response")
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise Channel3Unavailable(f"unexpected {what} in Channel3 response") from None
    if not value.is_finite() or value < 0:
        raise Channel3Unavailable(f"unexpected {what} in Channel3 response")
    return value


def _opt_decimal(raw: Any) -> Decimal | None:
    try:
        return _decimal(raw, "number") if raw is not None else None
    except Channel3Unavailable:
        return None


def parse_product(raw: Any) -> Product:
    """A ``Product`` from the wire shape. Offers without a usable price are skipped, not guessed."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise Channel3Unavailable("unexpected product in Channel3 response")
    offers: list[Offer] = []
    for item in raw.get("offers") or []:
        if not isinstance(item, dict) or not isinstance(item.get("price"), dict):
            continue
        price = item["price"]
        amount = price.get("price", price.get("amount"))
        try:
            value = _decimal(amount, "offer price")
        except Channel3Unavailable:
            continue
        offers.append(
            Offer(
                domain=normalize_domain(str(item.get("domain") or "")),
                url=str(item.get("url") or ""),
                price=value,
                currency=str(price.get("currency") or "").upper(),
                in_stock=item.get("availability") != "OutOfStock",
            )
        )
    return Product(id=raw["id"], title=str(raw.get("title") or ""), offers=tuple(offers))


def parse_history(raw: Any) -> PriceHistory:
    if not isinstance(raw, dict) or not isinstance(raw.get("canonical_product_id"), str):
        raise Channel3Unavailable("unexpected price history in Channel3 response")
    stats: dict[str, Any] = raw["statistics"] if isinstance(raw.get("statistics"), dict) else {}
    return PriceHistory(
        canonical_product_id=raw["canonical_product_id"],
        current_price=_opt_decimal(stats.get("current_price")),
        current_status=str(stats.get("current_status") or ""),
        mean=_opt_decimal(stats.get("mean")),
        min_price=_opt_decimal(stats.get("min_price")),
        max_price=_opt_decimal(stats.get("max_price")),
        currency=str(stats.get("currency") or "").upper(),
        points=len(raw.get("history") or []),
    )


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail[:160]
    return "invalid request" if detail else ""


def resolve_mode(api_key: str | None, mode: str | None) -> str:
    """``recorded`` when forced or when there is no key; ``live`` otherwise."""
    wanted = (mode or os.environ.get("CHANNEL3_MODE") or "").strip().lower()
    if wanted == "recorded":
        return "recorded"
    if wanted == "live":
        if not api_key:
            raise Channel3AuthError("CHANNEL3_MODE=live needs CHANNEL3_API_KEY")
        return "live"
    return "live" if api_key else "recorded"


@dataclass
class Channel3Client:
    api_key: str | None = field(default=None, repr=False)
    base_url: str = DEFAULT_BASE_URL
    mode: str = "recorded"
    timeout: float = DEFAULT_TIMEOUT
    fixtures_dir: Path = FIXTURES_DIR
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    calls: int = 0  # every request attempted, live or recorded; live ones cost credits (see credits_used)
    _http: httpx.Client | None = field(default=None, init=False, repr=False)
    _fixtures: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_env(
        cls,
        *,
        mode: str | None = None,
        transport: httpx.BaseTransport | None = None,
        fixtures_dir: Path | None = None,
    ) -> Channel3Client:
        key = os.environ.get("CHANNEL3_API_KEY") or None
        return cls(
            api_key=key,
            base_url=(os.environ.get("CHANNEL3_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            mode=resolve_mode(key, mode),
            transport=transport,
            fixtures_dir=fixtures_dir or FIXTURES_DIR,
        )

    # ------------------------------------------------------------------ public

    @property
    def credits_used(self) -> int:
        """Credits spent by this client: 1 per search or lookup call in live mode, 0 in recorded mode."""
        return self.calls if self.mode == "live" else 0

    def search(self, query: str, limit: int = 5) -> list[Product]:
        if not query.strip():
            raise Channel3RequestError("empty search query", 422)
        if self.mode == "recorded":
            self.calls += 1
            raw = self._fixture("search.json").get(query)
            return [parse_product(p) for p in raw or []]
        body = self._post("/v1/search", {"query": query, "limit": max(1, min(limit, 30))})
        products = body.get("products") if isinstance(body, dict) else None
        if not isinstance(products, list):
            raise Channel3Unavailable("unexpected search response from Channel3")
        return [parse_product(p) for p in products]

    def lookup(self, url: str) -> Product:
        if self.mode == "recorded":
            self.calls += 1
            raw = self._fixture("lookup.json").get(url)
            if raw is None:
                raise Channel3NotFound("Product not found", 404)
            return parse_product(raw)
        body = self._post("/v1/lookup", {"url": url})
        product = body.get("product") if isinstance(body, dict) else None
        if product is None:
            raise Channel3Unavailable("unexpected lookup response from Channel3")
        return parse_product(product)

    def price_history(self, canonical_product_id: str, days: int = 30) -> PriceHistory:
        days = max(1, min(days, 90))
        if self.mode == "recorded":
            self.calls += 1
            raw = self._fixture("history.json").get(canonical_product_id)
            if raw is None:
                raise Channel3NotFound(f"Canonical product {canonical_product_id} not found", 404)
            return parse_history(raw)
        return parse_history(self._request("GET", f"/v0/price-tracking/history/{canonical_product_id}", None, days))

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    # ---------------------------------------------------------------- internals

    def _fixture(self, name: str) -> dict[str, Any]:
        if name not in self._fixtures:
            path = self.fixtures_dir / name
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                msg = f"recorded Channel3 fixture {name} unreadable ({type(exc).__name__})"
                raise Channel3Unavailable(msg) from exc
            if not isinstance(data, dict):
                raise Channel3Unavailable(f"recorded Channel3 fixture {name} is not an object")
            self._fixtures[name] = data
        return self._fixtures[name]  # type: ignore[no-any-return]

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body, None)

    def _request(self, method: str, path: str, body: dict[str, Any] | None, days: int | None) -> Any:
        if not self.api_key:
            raise Channel3AuthError("CHANNEL3_API_KEY is not set", 401)
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport, follow_redirects=False
            )
        self.calls += 1
        try:
            response = self._http.request(
                method,
                path,
                json=body,
                params={"days": days} if days is not None else None,
                headers={"x-api-key": self.api_key, "accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise Channel3Unavailable(f"Channel3 unreachable ({type(exc).__name__})") from exc
        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError:
                raise Channel3Unavailable("Channel3 returned a non-JSON body", status) from None
        detail = _detail(response)
        if status == 401:
            raise Channel3AuthError(f"Channel3 rejected the API key: {detail}".rstrip(": "), status)
        if status == 402:
            raise Channel3CreditsError(f"Channel3 credits exhausted or payment required: {detail}".rstrip(": "), status)
        if status == 404:
            raise Channel3NotFound(detail or "not found", status)
        if status == 422:
            raise Channel3RequestError(detail or "invalid request", status)
        raise Channel3Unavailable(f"Channel3 error {status}: {detail}".rstrip(": "), status)


__all__ = [
    "Channel3AuthError",
    "Channel3Client",
    "Channel3CreditsError",
    "Channel3Error",
    "Channel3NotFound",
    "Channel3RequestError",
    "Channel3Unavailable",
    "Offer",
    "PriceHistory",
    "Product",
    "normalize_domain",
    "parse_history",
    "parse_product",
    "resolve_mode",
]
