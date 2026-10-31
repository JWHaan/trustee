"""Three A2A agents on three local ports complete one purchase, entirely over A2A.

    python examples/a2a_network.py                      # local Trustee (dry-run PayPal) + Northwind + Tidewater
    python examples/a2a_network.py --cheat              # Tidewater ships the junk twin: refunded, nothing captured
    python examples/a2a_network.py --trustee-url https://your-trustee.example.com --tidewater-public-url https://...

Northwind's planning agent and Tidewater's sales agent are separate servers with their own Agent Cards. Northwind
is given only Tidewater's address. It reads Tidewater's card, asks for a quote, negotiates with signed offers,
has Tidewater countersign the contract, registers at Trustee and opens the escrow. Trustee pushes "funds held"
to Tidewater, which sends the CSV to Northwind as an A2A file part. Northwind checks the bytes and attests.
Every hop is printed as it happens.

With `--trustee-url` only Northwind and Tidewater run locally. Trustee then has to reach Tidewater's push
webhook, so give `--tidewater-public-url` a URL it can reach (a tunnel such as ngrok); the local Trustee needs
nothing. The local Trustee always uses dry-run PayPal and never reads credentials.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from trustee.ledger import Ledger
from trustee.network.app import free_port, start_server
from trustee.network.common import Hop, Waiter, default_llm
from trustee.network.northwind import create_northwind
from trustee.network.tidewater import create_tidewater
from trustee.paypal import PayPalClient

PRINT_LOCK = threading.Lock()


def show(hop: Hop) -> None:
    with PRINT_LOCK:
        print("  " + hop.line(), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trustee-url", default=None, help="use this Trustee instead of starting a local one")
    parser.add_argument("--tidewater-public-url", default=None, help="where that Trustee can reach Tidewater's webhook")
    parser.add_argument("--mode", choices=["auto", "live", "scripted"], default="auto")
    parser.add_argument("--cheat", action="store_true", help="Tidewater ships the corrupt twin of the dataset")
    parser.add_argument("--operator-token", default=os.environ.get("TRUSTEE_OPERATOR_TOKEN"))
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for approval and delivery")
    args = parser.parse_args()

    from trustee.arena.api import create_app

    with PRINT_LOCK:
        print("A2A network on 127.0.0.1\n")
    servers: list[object] = []
    try:
        if args.trustee_url:
            trustee_url = args.trustee_url.rstrip("/")
        else:
            trustee = running_server_handle(create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True)), servers)
            trustee_url = trustee
            print(f"Trustee    {trustee_url}   (dry-run PayPal)")

        # The agents need their own URLs before they exist, so reserve ports first.
        tw_port, nw_port = free_port(), free_port()
        tw_url = args.tidewater_public_url or f"http://127.0.0.1:{tw_port}"
        nw_url = f"http://127.0.0.1:{nw_port}"
        llm = default_llm(args.mode)
        tidewater = create_tidewater(
            trustee_url=trustee_url,
            public_url=tw_url.rstrip("/"),
            cheat=args.cheat,
            operator_token=args.operator_token,
            llm=llm,
            observer=show,
        )
        northwind = create_northwind(
            trustee_url=trustee_url,
            public_url=nw_url,
            operator_token=args.operator_token,
            llm=llm,
            waiter=Waiter(poll_seconds=0.25, timeout_seconds=args.timeout),
            observer=show,
            on_approval=lambda deal, order, url: print(
                f"  >> approve the PayPal payment for {deal}: {url}", flush=True
            ),
        )
        running_server_handle(tidewater.app, servers, port=tw_port)
        running_server_handle(northwind.app, servers, port=nw_port)
        print(f"Tidewater  http://127.0.0.1:{tw_port}   {tidewater.identity.agent_id}")
        print(f"Northwind  {nw_url}   {northwind.identity.agent_id}\n")
        print("Northwind buys from Tidewater (it is told only Tidewater's address):")
        purchase = northwind.buy(f"http://127.0.0.1:{tw_port}")

        print()
        if purchase.outcome:
            print(f"stopped: {purchase.outcome}")
            return 1
        receipt = purchase.receipt or {}
        print(f"deal      {purchase.deal_id}  price {purchase.price} USD")
        print(f"receipt   {receipt.get('outcome')}  (capture {receipt.get('captureId', '-')})")
        print(f"closed by {receipt.get('closedBy')}")
        item = northwind.inbox.get(purchase.deal_id)
        if item is not None:
            print(f"inbox     {item.path}  sha256 {item.sha256[:16]}…  contract {item.contract_digest[:16]}…")
        return 0 if purchase.settled or (args.cheat and receipt.get("outcome") == "refunded") else 1
    finally:
        for handle in reversed(servers):
            handle.stop()  # type: ignore[attr-defined]


def running_server_handle(app: object, servers: list[object], port: int | None = None) -> str:
    """Start `app` on 127.0.0.1 and remember the handle so `main` can stop it. Returns its URL."""
    handle = start_server(app, "127.0.0.1", port)  # type: ignore[arg-type]
    servers.append(handle)
    return handle.url


if __name__ == "__main__":
    sys.exit(main())
