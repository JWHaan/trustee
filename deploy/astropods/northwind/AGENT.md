---
description: "Northwind Outfitters' buying agent. Negotiates over A2A and pays through Trustee escrow."
tags:
  - a2a
  - payments
  - escrow
  - procurement
authors:
  - name: Trustee
capabilities:
  - Finds a seller by its Agent Card and checks its Ed25519 identity
  - Negotiates a price with signed offers and counters
  - Opens a PayPal-backed escrow at Trustee and waits for payer approval
  - Checks the delivered file against the contract before Trustee releases the money
integrations:
  - PayPal
  - A2A
---

# Northwind planning agent

Buyer side of the Trustee demo. Every offer, contract and acceptance is signed, and money moves only through
Trustee (PayPal authorize, capture or void). The model negotiates; it cannot reach PayPal or choose an amount.

A2A endpoint: `/a2a`. Agent Card: `/.well-known/agent-card.json`.
