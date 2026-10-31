"""Real PayPal sandbox proof of concept: order -> payer approval -> authorize -> capture or void.

For a human with sandbox credentials (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET). It talks to PayPal
directly through the same `PayPalClient` Trustee uses, with no server involved:

    python examples/sandbox_poc.py                    # create, wait for approval, authorize, stop
    python examples/sandbox_poc.py --settle           # ... then capture to the seller
    python examples/sandbox_poc.py --refund           # ... then void (buyer keeps funds)
    python examples/sandbox_poc.py --dry-run          # no network, no credentials: shows the same steps

It prints the approve URL. Open it, log in with a SANDBOX buyer account and approve. It refuses to run
with PAYPAL_ENV=live and never prints credentials or tokens.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from decimal import Decimal

from trustee.paypal import PayPalClient, PayPalError

POLL_SECONDS = 3.0


def build_client(dry_run: bool) -> PayPalClient:
    env = os.environ.get("PAYPAL_ENV", "sandbox")
    if env == "live":
        raise SystemExit("refusing to run: PAYPAL_ENV=live. This proof of concept is sandbox only.")
    if dry_run:
        return PayPalClient(dry_run=True, requires_approval=True)
    client_id, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not (client_id and secret):
        raise SystemExit("set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app), or pass --dry-run")
    return PayPalClient(
        client_id=client_id,
        client_secret=secret,
        env="sandbox",
        dry_run=False,
        requires_approval=True,
        return_url=os.environ.get("PAYPAL_RETURN_URL", "https://example.com/paypal/return"),
        cancel_url=os.environ.get("PAYPAL_CANCEL_URL", "https://example.com/paypal/cancel"),
    )


def wait_for_approval(client: PayPalClient, order_id: str, timeout: float, poll: float) -> bool:
    """Poll the order until the payer approves. False on timeout or Ctrl-C."""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            status = str(client.get_order(order_id).get("status"))
            print(f"  order status: {status}")
            if status == "APPROVED":
                return True
            time.sleep(poll)
    except KeyboardInterrupt:
        print("interrupted while waiting; the order stays un-approved and nothing is held")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--amount", default="1.00", help="amount in USD (sandbox money)")
    parser.add_argument("--seller-email", default="seller-sandbox@trustee.dev", help="a sandbox business account")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for approval")
    parser.add_argument("--dry-run", action="store_true", help="no network: exercise the same steps locally")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--settle", action="store_true", help="capture the hold to the seller")
    group.add_argument("--refund", action="store_true", help="void the hold (buyer keeps funds)")
    args = parser.parse_args(argv)

    client = build_client(args.dry_run)
    run = uuid.uuid4().hex[:8]
    poll = 0.0 if args.dry_run else POLL_SECONDS
    try:
        order = client.create_and_authorize(
            amount=Decimal(args.amount),
            currency="USD",
            buyer_email="buyer-sandbox@trustee.dev",
            seller_email=args.seller_email,
            memo=f"trustee sandbox poc {run}",
            request_id=f"poc-{run}-create",
        )
        print(f"order id:   {order.order_id}")
        if order.status == "authorized":  # dry-run holds immediately
            print("(dry-run) funds held without payer approval")
        else:
            print(f"approve at: {order.approve_url}")
            if not wait_for_approval(client, order.order_id, args.timeout, poll):
                print("no approval; stopping. Nothing was held.")
                return 1
            order = client.authorize_after_approval(order.order_id, request_id=f"poc-{run}-authorize")
        print(f"authorization id: {order.authorization_id}  (funds held, not moved)")
        if args.settle:
            order = client.capture(order, request_id=f"poc-{run}-capture")
            print(f"captured: {order.details.get('capture_id') or order.details.get('id', '(see PayPal)')}")
        elif args.refund:
            order = client.void(order, reason="poc refund", request_id=f"poc-{run}-void")
            print("voided: the hold is released and the buyer keeps the funds")
        else:
            print("left authorized. Re-run with --settle or --refund (a new order is created each run).")
    except PayPalError as exc:
        print(f"PayPal error: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
