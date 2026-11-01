# Channel3 price-feed check

Northwind's merchandising agent buys a **competitor price feed** from Tidewater: a CSV of
`retailer, product_url, product_title, price, currency, observed_at`. A hash and a header check cannot tell a fresh
feed from a stale or invented one, so Northwind's attestation also checks a sample of the rows against live market
data from [Channel3](https://channel3.ai) before money is released. Everything here is opt-in; with the new env vars
unset, every existing scenario behaves as before.

## What is checked

1. `sha256 matches contract` and the header (`schema: retailer, product_url, product_title, price, currency, observed_at`)
   as for any good.
2. **Market check** (signed into the contract as the criterion `market check: 20 sampled rows priced within
   max(5%, $1) of live Channel3 offers`). The buyer samples 20 rows and marks a row valid when:
   - the product exists in Channel3: `POST /v1/lookup` for a row with a `product_url`, otherwise `POST /v1/search` on
     the title (the first product with an offer from the row's retailer wins);
   - that product has an offer from the row's `retailer` (matched on domain, `www.` ignored; in-stock offer first);
   - the currency matches the offer's currency;
   - `|feed price - market price| <= max(5% of market, 1.00)`. Exactly 5% passes; one cent over fails.

   All 20 sampled rows must be valid for the check to pass. Otherwise the detail is
   `failed: market check: N of 20 sampled rows not within tolerance ...`. It starts with `failed` and names the
   `rows` clause, so the arbiter treats it as an objective breach and voids, citing the signed market-check clause.
   Only Decimal arithmetic decides; no model is involved.

Not checked: `observed_at`. A feed is judged by its prices against the market now, not by its date column. Rows are
matched one retailer offer at a time; a feed priced for a different retailer than it names fails as
`no_offer_for_retailer`.

## Sampling

Row `i` gets the key `sha256("<contract digest>:<i>")`; the 20 smallest keys are the sample
(`trustee.pricefeed.sample_indices`). The contract digest is signed by both parties, so seller and buyer can recompute
the same sample, and the seller cannot know it before the contract exists. A feed shorter than 20 rows is checked in full.

## Partial settlement

If the contract carries the existing `partial` term, a failed market check also reports `units_valid` /
`units_total` over the **sampled** rows, and Trustee pays pro rata (`docs/PARTIAL_CAPTURE.md`) when the valid share is
at least `min_fraction`. The share is therefore an estimate of the feed's quality from a sample, not a count of every
row. The `pricefeed-mixed` scenario has 8 bad rows in 40, so any 20-row sample is at least 60% valid and always
settles pro rata (or in full, with probability about 0.2%, when no bad row is sampled).

If Channel3 itself cannot answer (bad key, no credits, outage), the detail is `unverified: ...` instead of `failed:
...`. The seller breached nothing, so the arbiter's objective-breach rule does not fire and no counts are reported.

## Evidence and tape

The attestation evidence gains `market_source` (`recorded` or `live`), `market_rows_sampled`, `market_rows_valid` and
`market_tolerance`. The tape gets one `policy` row per sampled check (rule `market_price`, result pass/fail, note
`row 6: feed 12.00 vs market 11.90 USD (ok), <product> @ <retailer>`).

Optional fair-price note: with `CHANNEL3_FAIR_PRICE=1`, the first resolved sampled product's
`GET /v0/price-tracking/history/{id}` is read and its `current_status` (low / typical / high) is added to the tape and
to `market_fair_price`. Channel3 only returns history for products you track (billed at 1 credit per product per day),
so a 404 is silently skipped. No mandate rule uses it.

## Recorded vs live

| Variable | Meaning |
|---|---|
| `CHANNEL3_API_KEY` | Sets live mode. Sent as the `x-api-key` header, never logged or returned. |
| `CHANNEL3_BASE_URL` | Default `https://api.trychannel3.com`. |
| `CHANNEL3_MODE` | `recorded` forces fixtures even with a key; `live` demands a key. |
| `CHANNEL3_FAIR_PRICE` | `1` adds the price-history note. |
| `ARENA_PRICEFEED_SCENARIO` | `1` lists the three scenarios in `/api/scenarios` (they always run by name). |

With no key the client serves `src/trustee/arena/fixtures/channel3/{lookup,search,history}.json`. **That snapshot is
synthetic**: it follows the response shapes in Channel3's API reference, uses `.example` retailer domains and was not
captured from the live API. The demo feeds are built from it by
`uv run python -m trustee.arena.fixtures.build_pricefeed` (deterministic; the sha256 values in `scenarios.json` are
those of the committed files). The synthetic products do not exist in the real catalog, so with a key set the demo
feeds fail the check, as they should. For a live run, supply a feed of real product URLs (see below).

## Credit cost

Free tier: 1,000 credits per month; extra credits $0.007 each ([pricing](https://channel3.mintlify.dev/docs/pricing.md)).
A live check costs **one credit per sampled row** (lookup or search), so **20 credits per delivery**, about 50 runs per
month on the free tier ($0.14 per run beyond it). Price history, when enabled, is read at no cost for tracked
products, but tracking itself is 1 credit per product per day. Recorded mode costs nothing.

## Running it

```
uv run python -m trustee.arena.fixtures.run_honest pricefeed        --mode scripted   # SETTLED
uv run python -m trustee.arena.fixtures.run_honest pricefeed-stale  --mode scripted   # VOIDED, cites the market clause
uv run python -m trustee.arena.fixtures.run_honest pricefeed-mixed  --mode scripted   # SETTLED pro rata
ARENA_PRICEFEED_SCENARIO=1 uv run uvicorn ...                                         # show them in the UI
```

On the A2A path, `create_northwind(good=get_scenario("pricefeed").good)` already carries the checker and counter
(`default_checkers()` / `default_counters()`), and `trustee.network.tidewater` serves the same files.

## Going live

1. Create a Channel3 account and API key, set `CHANNEL3_API_KEY` in the environment of the process that runs the buyer
   (Northwind). Nothing else changes.
2. Give the seller a real feed: the same six columns, `retailer` a store domain (`bestbuy.com`), `product_url` a real
   product page at that store (or blank with a precise `product_title`), prices in the retailer's currency. Put the file
   in `src/trustee/arena/fixtures/goods/`, and name its sha256 in a `GoodSpec` with
   `good_id=competitor-price-feed-2026q4` and the criterion text from `trustee.pricefeed.CRITERION`.

## Risks and limits

- Live prices drift, and a retailer's price at lookup time can differ from the seller's observation minutes earlier;
  the 5% / $1 tolerance absorbs ordinary movement, but a feed full of items on flash sale will fail. The `partial` term
  is the intended cushion.
- `/v1/lookup` of a product outside Channel3's catalog fetches the page live and may fail (`500`/`504`); that is an
  `unverified` outcome, not a seller breach.
- Search-by-title rows can match a different but similarly named product at the retailer; prefer rows with URLs.
- The pay-per-call (MPP/Tempo) path and Channel3 checkout are not used.
