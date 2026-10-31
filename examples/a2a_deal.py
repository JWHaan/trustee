"""Two independent agents complete an escrowed deal over HTTP.

Each agent has its own key and its own principal; neither shares memory with the other or
with the server. Start a service first:

    python -m uvicorn trustee.arena.api:app --host 127.0.0.1 --port 8000

then run:

    python examples/a2a_deal.py --base-url http://127.0.0.1:8000

Dry-run PayPal (the default without credentials) settles immediately. With real sandbox
credentials the escrow waits for the buyer to approve the order: the script prints the
approval link and stops.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from trustee.client import TrusteeClient
from trustee.identity import AgentKey
from trustee.mandate import Mandate
from trustee.protocol import Attest, Contract, Delivery, GoodSpec

DELIVERABLE = b"def handler(event):\n    return {'ok': True}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--operator-token", default=None, help="needed only if the server gates registration")
    args = parser.parse_args()

    run = uuid.uuid4().hex[:8]
    buyer_id, seller_id = f"buyer-{run}", f"seller-{run}"
    buyer_org, seller_org = f"acme-{run}", f"globex-{run}"
    client = TrusteeClient(args.base_url, operator_token=args.operator_token)

    card = client.agent_card()
    print(f"discovered {card['name']} v{card['version']} with skills: {', '.join(s['id'] for s in card['skills'])}")

    # Separate keys: the principals (humans/companies) and the agents they authorize.
    buyer_org_key, seller_org_key = AgentKey.generate(), AgentKey.generate()
    buyer_key, seller_key = AgentKey.generate(), AgentKey.generate()

    client.register_principal(buyer_org, buyer_org_key)
    client.register_principal(seller_org, seller_org_key)
    client.register_agent(buyer_id, buyer_key, buyer_org, buyer_org_key, ["buyer"])
    client.register_agent(seller_id, seller_key, seller_org, seller_org_key, ["seller"])
    print(f"registered {buyer_id} (principal {buyer_org}) and {seller_id} (principal {seller_org})")

    categories = frozenset({"code"})
    client.publish_mandate(
        Mandate(
            agent_id=buyer_id,
            principal=buyer_org,
            max_amount=Decimal("100.00"),
            allowed_categories=categories,
            allowed_counterparties=frozenset({seller_id}),
            grants="spend",
        ),
        buyer_org_key,
    )
    client.publish_mandate(
        Mandate(
            agent_id=seller_id,
            principal=seller_org,
            max_amount=Decimal("100.00"),
            allowed_categories=categories,
            allowed_counterparties=frozenset({buyer_id}),
            grants="receive",
        ),
        seller_org_key,
    )
    print("mandates published")

    now = datetime.now()
    contract = Contract(
        deal_id=f"deal-{run}",
        buyer=buyer_id,
        seller=seller_id,
        good=GoodSpec(
            good_id="lambda-module",
            description="A small lambda handler module",
            category="code",
            acceptance_criteria=["sha256 matches the contract"],
            deliverable_ref="lambda-module.py",
            sha256=hashlib.sha256(DELIVERABLE).hexdigest(),
        ),
        price=Decimal("25.00"),
        deadline=now + timedelta(hours=1),
        formed_at=now,
    )
    contract = contract.sign(buyer_id, buyer_key).sign(seller_id, seller_key)  # both parties sign the same digest

    deal = client.open_deal(contract)
    print(f"escrow: state={deal['state']} order={deal['escrow']['order_id']} status={deal['escrow']['status']}")
    if deal["state"] != "ESCROWED":
        print(f"funds are not held yet; the buyer must approve in PayPal: {deal['escrow'].get('approve_url')}")
        return 2

    delivery = Delivery(
        sender=seller_id,
        deal_id=contract.deal_id,
        deliverable_ref=contract.good.deliverable_ref,
        content_hash=hashlib.sha256(DELIVERABLE).hexdigest(),
    ).sign(seller_key)
    print(f"delivered: state={client.deliver(delivery)['state']}")

    # The buyer hashes the bytes it received itself, then attests.
    received_hash = hashlib.sha256(DELIVERABLE).hexdigest()
    matches = received_hash == contract.good.sha256
    attest = Attest(
        sender=buyer_id,
        deal_id=contract.deal_id,
        verdict="accept" if matches else "reject",
        reason="hash matches the contract" if matches else "hash mismatch",
        evidence={"sha256_check": "match" if matches else "mismatch"},
    ).sign(buyer_key)
    final = client.attest(attest)

    print(f"final: state={final['state']} escrow={final['escrow']['status']}")
    for reason, frm, to in final["history"]:
        print(f"  {frm} -> {to}: {reason}")
    return 0 if final["state"] == "SETTLED" else 1


if __name__ == "__main__":
    sys.exit(main())
