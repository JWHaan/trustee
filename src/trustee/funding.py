"""Funding sources — the one-time PayPal Vault consent behind an intent mandate.

The principal approves ONE PayPal consent screen (a Vault setup token). Trustee turns it into a payment token, whose
id is a PayPal *vault id*: with it Trustee can authorize future orders for that payer with no approval click. That
makes the vault id a bearer secret, so this module is built around not letting it out:

  * clients only ever hold a `funding_ref`, a random handle with no meaning at PayPal;
  * the vault id lives in the store, and `FundingRegistry.vault_id` is the single reader; it hands the id to the
    PayPal adapter and nowhere else;
  * no API response, ledger row, log line, receipt or error message built here contains it, and a funding source is
    bound to its principal, so another principal's intent cannot name it.

Dry-run (no PayPal credentials) uses fake ids end to end, so the flow is identical offline.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from typing import Any

from .paypal import PayPalClient, PayPalError
from .store import Store

log = logging.getLogger(__name__)

DEMO_VAULT_ENV = "PAYPAL_DEMO_VAULT_ID"


class FundingError(Exception):
    """A funding operation failed in a way the caller can report. `status` is an HTTP-ish code."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def new_funding_ref() -> str:
    return "fund_" + secrets.token_urlsafe(12)


class FundingRegistry:
    """Funding sources in the store, plus the PayPal calls that create them. Implements `intent.FundingResolver`."""

    def __init__(self, store: Store, paypal: PayPalClient):
        self.store = store
        self.paypal = paypal
        self._lock = threading.RLock()

    # -- the one-time consent ----------------------------------------------------------------------------------

    def begin(self, principal: str, funding_ref: str | None = None) -> dict[str, Any]:
        """Ask PayPal for a consent link. The principal opens `approve_url` once and approves.

        A principal may choose its own `funding_ref`. Asking again for one it already owns is idempotent: an active
        source is returned as it is, a pending one gets a fresh consent link. A ref owned by anyone else is a 409."""
        ref = funding_ref or new_funding_ref()
        existing = self.store.funding_public(ref)
        if existing is not None:
            if existing["principal"] != principal:
                raise FundingError(409, f"funding_ref {ref!r} is already taken")
            if existing["revoked"]:
                raise FundingError(409, f"funding source {ref!r} was revoked; choose a new funding_ref")
            if existing["vaulted"]:
                return {**self._public(existing), "approve_url": None}
        try:
            token = self.paypal.create_setup_token()
        except PayPalError as exc:
            raise FundingError(502, f"PayPal could not start the consent: {exc}") from None
        self.store.put_funding(ref, principal, token["setup_token_id"], None)
        return {
            "funding_ref": ref,
            "principal": principal,
            "approve_url": token["approve_url"],
            "status": "consent_required",
        }

    def complete(self, setup_token_id: str) -> dict[str, Any]:
        """The payer came back from PayPal: if they approved, vault the payment method. Idempotent."""
        ref = self.store.funding_by_setup_token(setup_token_id)
        if ref is None:
            raise FundingError(404, "unknown consent token")
        with self._lock:
            info = self.store.funding_public(ref)
            assert info is not None
            if info["revoked"]:
                raise FundingError(409, "this funding source was revoked")
            if info["vaulted"]:
                return self._public(info)
            try:
                status = str(self.paypal.get_setup_token(setup_token_id).get("status"))
                if status != "APPROVED":
                    raise FundingError(409, f"the consent is not approved yet (status {status})")
                vault_id = self.paypal.create_payment_token(setup_token_id)
            except PayPalError as exc:
                raise FundingError(502, f"PayPal could not finish the consent: {exc}") from None
            self.store.put_funding(ref, info["principal"], setup_token_id, vault_id)
        info = self.store.funding_public(ref)
        assert info is not None
        return self._public(info)

    def register_demo(self, principal: str, vault_id: str | None = None) -> str | None:
        """Pre-register a vault id (from PAYPAL_DEMO_VAULT_ID) so hosted demo runs are autonomous. Returns the ref."""
        vault_id = vault_id or os.environ.get(DEMO_VAULT_ENV) or None
        if not vault_id:
            return None
        ref = "fund_demo_" + principal
        self.store.put_funding(ref, principal, None, vault_id)
        return ref

    def register_fake(self, principal: str) -> str:
        """Dry-run funding: a source with a fake vault id, so offline runs take the same autonomous path."""
        ref = new_funding_ref()
        self.store.put_funding(ref, principal, None, f"dry-vault-{secrets.token_hex(6)}")
        return ref

    # -- lifecycle ------------------------------------------------------------------------------------------------

    def revoke(self, principal: str, funding_ref: str) -> dict[str, Any]:
        info = self.store.funding_public(funding_ref)
        if info is None or info["principal"] != principal:
            raise FundingError(404, f"no funding source {funding_ref!r} for principal {principal!r}")
        self.store.revoke_funding(funding_ref)
        return {"funding_ref": funding_ref, "revoked": True}

    def status(self, funding_ref: str) -> dict[str, Any]:
        info = self.store.funding_public(funding_ref)
        if info is None:
            raise FundingError(404, f"unknown funding source {funding_ref!r}")
        return self._public(info)

    # -- FundingResolver -------------------------------------------------------------------------------------------

    def is_active(self, principal: str, funding_ref: str) -> bool:
        return self.store.get_vault_id(funding_ref, principal) is not None

    def vault_id(self, principal: str, funding_ref: str) -> str | None:
        """Internal: the PayPal vault id, for `PayPalClient.create_and_authorize` only."""
        return self.store.get_vault_id(funding_ref, principal)

    @staticmethod
    def _public(info: dict[str, Any]) -> dict[str, Any]:
        state = "revoked" if info["revoked"] else ("active" if info["vaulted"] else "consent_required")
        return {"funding_ref": info["funding_ref"], "principal": info["principal"], "status": state}
