"""Verify a Trustee settlement receipt offline.

    python -m trustee.verify receipt.json --key=<base64 public key | https://host/.well-known/trustee-key.json>
        [--contract contract.json] [--intent intent.json] [--events events.json]

What is checked:
  * the receipt's Ed25519 signature, against the key you pass (pin it: a key read out of the receipt itself only
    proves the receipt is self-consistent, and the report says so);
  * with `--contract` / `--intent`: that those documents hash to the digests the receipt commits to, and the intent is
    signed by its principal if `--principal-key` is given;
  * with `--events`: that the deal's ledger is an unbroken hash chain, and that the receipt's `ledger_head` is the hash
    of the event at position `ledger_events` (so the receipt covers exactly that history).

Exit status 0 only when every requested check passes. Nothing here talks to PayPal or needs the server's database.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from .identity import verify as verify_signature
from .intent import IntentMandate
from .ledger import verify_chain
from .protocol import Contract
from .receipts import RECEIPT_TYPE, receipt_signing_bytes


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]}


def verify_receipt(
    receipt: dict[str, Any],
    *,
    public_key: str | None = None,
    contract: Contract | dict[str, Any] | None = None,
    intent: IntentMandate | dict[str, Any] | None = None,
    principal_key: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> VerifyReport:
    """Run every check the supplied evidence allows. Never raises on malformed input: it reports a failed check."""
    report = VerifyReport()
    if receipt.get("type") != RECEIPT_TYPE:
        report.add("receipt.type", False, f"expected {RECEIPT_TYPE}")
        return report
    key = public_key or str(receipt.get("trustee_key") or "")
    pinned = public_key is not None
    if pinned and receipt.get("trustee_key") != public_key:
        report.add("signature.key_matches", False, "the receipt names a different Trustee key than the one you pinned")
    sig_ok = bool(key) and verify_signature(key, str(receipt.get("signature") or ""), receipt_signing_bytes(receipt))
    report.add(
        "signature.trustee",
        sig_ok,
        ("valid against the pinned key" if pinned else "valid against the key inside the receipt (pin it with --key)")
        if sig_ok
        else "invalid",
    )

    if contract is not None:
        try:
            c = contract if isinstance(contract, Contract) else Contract.model_validate(contract)
            report.add("contract.digest", c.digest() == receipt.get("contract_digest"), c.digest()[:16])
        except ValueError as exc:
            report.add("contract.digest", False, f"unreadable contract: {str(exc)[:80]}")
    if intent is not None:
        try:
            i = intent if isinstance(intent, IntentMandate) else IntentMandate.model_validate(intent)
            report.add("intent.digest", i.digest() == receipt.get("intent_mandate_digest"), i.digest()[:16])
            if principal_key:
                ok = bool(i.principal_signature) and verify_signature(
                    principal_key, i.principal_signature or "", i.signing_bytes()
                )
                report.add("intent.principal_signature", ok, f"principal {i.principal}")
        except ValueError as exc:
            report.add("intent.digest", False, f"unreadable intent: {str(exc)[:80]}")
    if events is not None:
        ok, why = verify_chain(events)
        report.add("ledger.chain", ok, why)
        if ok:
            count = int(receipt.get("ledger_events") or 0)
            head = events[count - 1].get("hash") if 0 < count <= len(events) else None
            report.add(
                "ledger.head",
                head is not None and head == receipt.get("ledger_head"),
                f"receipt covers {count} of {len(events)} events",
            )
    if not receipt.get("human_present", True):
        policy = receipt.get("policy")
        rows = policy if isinstance(policy, list) else []
        failed = [
            p.get("rule") if isinstance(p, dict) else "?" for p in rows if not (isinstance(p, dict) and p.get("ok"))
        ]
        if not isinstance(policy, list):
            failed.append("policy")
        report.add("policy.all_passed", not failed, ", ".join(map(str, failed)) or "every rule passed")
    return report


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _fetch_key(spec: str) -> str:
    if spec.startswith(("http://", "https://")):
        import httpx

        data = httpx.get(spec, timeout=15.0).json()
        return str(data["public_key"])
    return spec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trustee.verify", description="Verify a Trustee settlement receipt.")
    ap.add_argument("receipt", help="receipt JSON file")
    ap.add_argument(
        "--key",
        help="Trustee public key (base64) or a URL of /.well-known/trustee-key.json. Write --key=KEY for a raw key: "
        "a base64url key can start with '-', which argparse would otherwise read as an option",
    )
    ap.add_argument("--contract", help="contract JSON; its digest must match the receipt")
    ap.add_argument("--intent", help="intent mandate JSON; its digest must match the receipt")
    ap.add_argument("--principal-key", help="principal public key (base64): also check the intent's signature")
    ap.add_argument("--events", help="the deal's events JSON (GET /v1/deals/{id}/events): check the hash chain")
    args = ap.parse_args(argv)
    try:
        receipt = _load(args.receipt)
        events = _load(args.events) if args.events else None
        if isinstance(events, dict):
            events = events.get("events")
        report = verify_receipt(
            receipt,
            public_key=_fetch_key(args.key) if args.key else None,
            contract=_load(args.contract) if args.contract else None,
            intent=_load(args.intent) if args.intent else None,
            principal_key=args.principal_key,
            events=events,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot verify: {exc}", file=sys.stderr)
        return 2
    for c in report.checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.name}  {c.detail}")
    print("VERIFIED" if report.ok else "NOT VERIFIED")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
