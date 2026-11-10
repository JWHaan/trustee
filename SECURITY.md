# Security policy

Trustee holds and moves money on behalf of other people's agents, so security reports are welcome.

## Scope

Trustee is a hackathon project, not production software. Use it only in dry-run mode or against the PayPal sandbox,
which uses test money. The sandbox proof of concept (`examples/sandbox_poc.py`) refuses to run with
`PAYPAL_ENV=live`, but the server itself has no such guard, so never give it live PayPal credentials.

The most useful reports show how to break one of Trustee's guarantees, for example:

- holding or moving money without the signatures and mandates a deal requires;
- getting paid for a delivery that fails the contract, or refunded for one that passes;
- capturing, voiding or refunding a payment twice;
- passing as another agent, or replaying an old mandate;
- changing the ledger, a receipt or an evidence pack without it being detected.

[What Trustee stops](README.md#what-trustee-stops) lists the attacks the test suite already covers.

## Reporting a vulnerability

Please report privately: open the [Security tab](https://github.com/JWHaan/trustee/security) and choose
**Report a vulnerability**. Do not open a public issue for a vulnerability.

Include the steps to reproduce it and what you expected to happen. This is a personal project, so there is no bug
bounty, but every report is read.

Never use real payment credentials or real money to test Trustee.
