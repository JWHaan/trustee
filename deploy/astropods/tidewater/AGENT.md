---
description: "Tidewater Data Co's selling agent. Quotes, negotiates and ships a dataset once Trustee holds the funds."
tags:
  - a2a
  - payments
  - escrow
  - data-sales
authors:
  - name: Trustee
capabilities:
  - Publishes a quote and negotiates with signed counters
  - Countersigns the contract and registers at Trustee
  - Ships the CSV as an A2A file part after Trustee reports the funds held
integrations:
  - PayPal
  - A2A
---

# Tidewater sales agent

Seller side of the Trustee demo. With `TIDEWATER_CHEAT=1` it ships a corrupt twin of the dataset, which the buyer
rejects and Trustee refunds.

A2A endpoint: `/a2a`. Agent Card: `/.well-known/agent-card.json`.
