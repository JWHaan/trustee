# Trustee

An escrow + arbitration agent for agent-to-agent commerce — PayPal AI Hackathon entry.

In an agent-to-agent world, two agents can negotiate anything and enforce nothing.
Trustee is the missing trust layer: when buyer and seller agents form a machine
contract, the Trustee gates it against both parties' **mandates** (AP2-lite:
budget caps, counterparty allowlists, TTLs), holds the money in **PayPal escrow**
(Orders authorize/capture/void, sandbox), settles on delivery + attestation, and
auto-unwinds (voids + refunds) when the deal fails. Every decision lands in an
append-only **ledger** that a live arena UI will tape out in AG Grid.

## Money path

```
contract formed
  -> mandate gate (no LLM: hard policy)        rejected? deal dies, money untouched
  -> PayPal Orders.create + authorize           funds HELD, not moved
  -> seller agent delivers (sha256 recorded)
  -> buyer agent attests
       accept -> capture  (settled to seller)
       reject -> arbiter -> void (refund)  |  deadline lapse -> auto-void
```

The LLM proposes and explains; the policy layer is the only thing that can
touch PayPal. Terminal states are immutable; webhook replays are idempotent
no-ops — funds can never double-settle or strand.

## Layout

```
src/trustee/
  protocol.py      offer/contract JSON protocol (the only thing settlement sees)
  mandate.py       AP2-lite mandate engine
  state.py         deal state machine (guarded, idempotent)
  paypal.py        PayPal sandbox adapter (authorize/capture/void + dry-run mode)
  ledger.py        append-only JSONL event ledger
  llm.py           OpenAI-compatible client (NIM/OpenAI/Ollama)
  agents/trustee.py  the Trustee agent
tests/             end-to-end flow tests (honest settle / cheat refund / gate / timeout)
```

## Quickstart

```bash
uv sync --extra dev
cp .env.example .env        # add PAYPAL sandbox creds + LLM keys when ready
uv run pytest              # dry-run mode works with zero credentials
```

`.env` knobs: `PAYPAL_CLIENT_ID/SECRET` (empty = dry-run), `LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL` (NIM-compatible by default).

## Demo plan (≤3 min)

1. **Honest run** — agents negotiate, contract, escrow, delivery, attested,
   settled. Zero human money-touch.
2. **Cheat run** — seller ships a junk deliverable, buyer rejects, arbiter
   cites the clause, auto-void + refund. The reason the trust layer exists.

## License

Apache-2.0 (see LICENSE).
