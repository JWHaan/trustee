# Partial capture and reauthorization

Two opt-in features on the money path. With neither switched on, Trustee behaves exactly as before.

## 1. Pro-rata settlement

### The contract term

A contract may carry an optional `partial` term. Both parties sign it, because it is part of the contract digest.

```json
"partial": {"basis": "units", "min_fraction": "0.5"}
```

| Field | Meaning |
|---|---|
| `basis` | What is counted. Only `units` (rows, items) exists. |
| `min_fraction` | Smallest valid fraction, `0 < x <= 1`, that still earns a pro-rata payment. Inclusive. |

When `partial` is unset it is left out of the digest, so contracts signed before this feature keep the same digest
and signatures.

### What happens on a reject

1. The buyer's attestation (`verdict: reject`) carries counts its own checker measured on the bytes it received:
   `evidence.units_valid` and `evidence.units_total` (decimal digit strings). `BuyerAgent` adds them only when the
   contract has a `partial` term, the delivery failed, and a unit counter is registered for the good. The arena
   registers `count_returns_units` for the returns dataset.
2. Trustee computes, with `Decimal` only: `captured = floor_to_cent(price * units_valid / units_total)` in exact
   integer-cent arithmetic. `released = price - captured`.
3. If `0 < units_valid < units_total`, `units_valid / units_total >= min_fraction` and `captured > 0`, Trustee
   captures `captured` with `final_capture: true`. PayPal releases the rest of the hold to the buyer. The deal goes
   `DELIVERED -> SETTLED`; no state was added.
4. Anything else (no term, counts missing or malformed, zero total, valid greater than total, nothing valid, every
   unit valid, below `min_fraction`) takes the unchanged reject path: arbiter, then void.

Neither the arbiter nor an LLM picks the amount. A mandate that lapsed since escrow parks the deal in `DISPUTED`
with the funds held, as for a full capture. An operator `settle` override captures the full price.

### PayPal call

`POST /v2/payments/authorizations/{authorization_id}/capture` with

```json
{"amount": {"currency_code": "USD", "value": "140.40"}, "final_capture": true}
```

and the usual deterministic `PayPal-Request-Id` (`trustee-<deal>-capture`). `PayPalClient.capture(order, amount=None,
currency="USD", final_capture=True)` sends the old empty request when `amount` is `None`.

### Where it shows

- Ledger: a `paypal` row (`op: capture`, `amount`) and a `settlement` row (`event: partial_settlement`, `basis`,
  `units_valid`, `units_total`, `captured`, `released`, `price`, note "settled 90% (140.40 of 156.00 USD); ...").
  The arena tape renders both without UI changes.
- `GET /v1/deals/{id}` has a `settlement` object (only when a partial settlement happened) and the contract clauses
  list `partial`.
- A2A: the task status text reads "Settled pro-rata: ...", `metadata.trustee.settlement` holds the object, and the
  `settlement-receipt` artifact carries `amount` (what moved), `contractAmount`, `released` and `settlement`.
- Mandate budgets: only `captured` counts as spend, and the remainder goes back to the buyer's `max_total`. A restart
  rebuilds spend from the stored settlement.

### Arena scenario

`partial` ("Partial delivery"): Tidewater ships `returns-2026q3-partial.csv`, the file the contract's sha256 names.
500 of its 5,000 rows have a blank `refund_usd`, so Northwind's row checks fail, 4,500 are valid, and at 156.00 USD
Trustee captures 140.40 and releases 15.60 (dry-run PayPal).

```
uv run python -m trustee.arena.fixtures.run_honest partial --mode scripted
```

The scenario is always runnable, but the arena lists it in `/api/scenarios` (and so in the UI) only with
`ARENA_PARTIAL_SCENARIO=1`, because existing tests pin the list of five.

## 2. Reauthorization

PayPal honors an authorization for 3 days. From day 4 until day 29 it can be reauthorized, which re-checks the
funds and returns a new authorization id.

- `PayPalClient.reauthorize(order)` calls `POST /v2/payments/authorizations/{id}/reauthorize` and swaps the new id
  into the `EscrowOrder` (the old one is kept in `details.previous_authorization_id`), so later capture and void use
  the new id. Dry-run is supported.
- `Trustee.reauthorize(contract, now)` renews a held authorization at most once per 3-day period, only while the
  contract deadline is still ahead and the original 29 days are open. The request id counts renewals
  (`trustee-<deal>-reauthorize-<n>`), so a retry is deduplicated. A `paypal` ledger row (`op: reauthorize`) records
  old and new ids.
- `DeadlineSweeper` calls it after `tick()` found nothing to do, when `TRUSTEE_REAUTHORIZE=1` (default off). On a
  PayPal failure the error is ledgered, the hold and the deadline/void behavior are untouched, and the next attempt
  waits one hour.

The 29-day limit still counts from the original authorization; reauthorizing does not extend it.

## Not built

Milestone payments (capture with `final_capture: false` except the last). A deal would have to stay open after a
capture, which needs new `DealState` transitions out of `SETTLED` (terminal today). `PayPalClient.capture` already
accepts `final_capture=False` for when that is added.
