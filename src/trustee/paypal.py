"""PayPal sandbox adapter — the only module that talks to PayPal.

Client-credentials REST against the sandbox:
  * Orders.create (intent=AUTHORIZE) -> authorize   = funds HELD, not moved
  * Orders.capture                        = settle to seller (all of it, or a final partial amount)
  * Authorizations.reauthorize            = renew the hold after the 3-day honor period
  * Orders.void                           = release the hold (refund)

Design notes:
  * `dry_run=True` (or missing credentials) keeps the same call surface with
    locally-generated order/authorization ids — tests and the first demo
    runs never need live sandbox creds, and the judge-run path is identical.
  * Retries with exponential backoff on 429/5xx and on transport errors; all
    failures raise PayPalError with the response body for the ledger.
  * Every money POST carries a `PayPal-Request-Id`. One id is fixed per logical
    operation and reused across its retries (and by the Trustee across process
    restarts), so a request whose response was lost can be replayed without PayPal
    authorizing, capturing or voiding twice.
  * The OAuth token is refreshed before it expires and once after a 401.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN = 60.0  # refresh this many seconds before expiry
_REQUEST_ID_MAX = 107  # PayPal-Request-Id must stay under 108 chars

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

DEFAULT_BUYER_EMAIL = "buyer-sandbox@trustee.dev"
DEFAULT_SELLER_EMAIL = "seller-sandbox@trustee.dev"


def sandbox_emails() -> tuple[str, str]:
    """(buyer, seller) PayPal emails for demo deals: env PAYPAL_BUYER_EMAIL / PAYPAL_SELLER_EMAIL.

    The seller email is the order's payee, so against the real sandbox it must be one of your sandbox
    *business* accounts. The placeholder default only works in dry-run: PayPal accepts the order and
    the payer's approval, then refuses to authorize it, and after a few attempts locks the order with
    MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED.
    """
    return (
        os.environ.get("PAYPAL_BUYER_EMAIL") or DEFAULT_BUYER_EMAIL,
        os.environ.get("PAYPAL_SELLER_EMAIL") or DEFAULT_SELLER_EMAIL,
    )


class PayPalError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


@dataclass
class EscrowOrder:
    order_id: str
    authorization_id: str | None = None
    status: str = "created"  # created | authorized | captured | voided
    buyer_payer_id: str = ""
    approve_url: str | None = None
    details: dict = field(default_factory=dict)


class PayPalClient:
    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        env: str = "sandbox",
        dry_run: bool | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        requires_approval: bool | None = None,
        return_url: str = "https://example.com/paypal/return",
        cancel_url: str = "https://example.com/paypal/cancel",
        webhook_id: str | None = None,
        allow_unverified_webhooks: bool | None = None,
        vault_return_url: str | None = None,
        vault_cancel_url: str | None = None,
    ):
        self.env = env
        self.base_url = base_url or BASE_URLS[env]
        # `client_id and client_secret` returns "" when both are empty — which is
        # falsy but *not* False, so `if self.dry_run:` still went to the network
        # and 401'd. The no-creds-means-dry-run promise needs a real bool.
        self.dry_run = (not (client_id and client_secret)) if dry_run is None else bool(dry_run)
        # Real sandbox always needs payer approval before an AUTHORIZE order can
        # be authorized, so that is the default whenever we are talking to it.
        self.requires_approval = (not self.dry_run) if requires_approval is None else bool(requires_approval)
        self.return_url = return_url
        self.webhook_id = webhook_id if webhook_id is not None else (os.environ.get("PAYPAL_WEBHOOK_ID") or None)
        # Dev only: lets a dry-run server accept hand-made webhook posts. Never honored against live PayPal.
        self.allow_unverified_webhooks = (
            os.environ.get("TRUSTEE_WEBHOOK_ALLOW_UNVERIFIED") == "1"
            if allow_unverified_webhooks is None
            else bool(allow_unverified_webhooks)
        )
        self.cancel_url = cancel_url
        # Where PayPal sends the payer after the one-time vault consent (see platform/routes.py).
        self.vault_return_url = vault_return_url or return_url.replace("/paypal/return", "/funding/return")
        self.vault_cancel_url = vault_cancel_url or cancel_url.replace("/paypal/cancel", "/funding/cancel")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expiry: float = 0.0  # time.monotonic() deadline
        self._http = httpx.Client(timeout=timeout)
        self._max_retries = max_retries

    # ------------------------------------------------------------------ http

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if not self.dry_run:
            h["Authorization"] = f"Bearer {self._get_token()}"
        if request_id:
            h["PayPal-Request-Id"] = request_id[:_REQUEST_ID_MAX]
        return h

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry - _TOKEN_REFRESH_MARGIN:
            return self._token
        try:
            resp = self._http.request(
                "POST",
                f"{self.base_url}/v1/oauth2/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
        except httpx.TransportError as exc:
            raise PayPalError(f"token endpoint unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise PayPalError(f"token endpoint failed ({resp.status_code})", resp.status_code, resp.text[:500])
        data = resp.json()
        token = str(data["access_token"])
        self._token = token
        self._token_expiry = time.monotonic() + float(data.get("expires_in", 300))
        return token

    def _drop_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    def _request_with_retry(
        self, method: str, url: str, json_body: dict | None = None, request_id: str | None = None
    ) -> httpx.Response:
        """Send with retries. A POST gets ONE request id for all its attempts."""
        if method == "POST" and request_id is None:
            request_id = uuid.uuid4().hex
        last: httpx.Response | None = None
        last_exc: httpx.TransportError | None = None
        refreshed = False
        attempt = 0
        while attempt <= self._max_retries:
            try:
                resp = self._http.request(method, url, json=json_body, headers=self._headers(request_id))
            except httpx.TransportError as exc:  # timeouts included
                last_exc, last = exc, None
                if attempt < self._max_retries:
                    time.sleep(0.5 * 2**attempt)
                attempt += 1
                continue
            if resp.status_code == 401 and not refreshed and not self.dry_run:
                refreshed = True
                self._drop_token()
                continue
            if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < self._max_retries:
                last, last_exc = resp, None
                time.sleep(0.5 * 2**attempt)
                attempt += 1
                continue
            return resp
        if last_exc is not None:
            raise PayPalError(
                f"PayPal request outcome unknown after retries ({type(last_exc).__name__}); "
                f"safe to retry with the same request id {request_id}"
            ) from last_exc
        assert last is not None
        raise PayPalError(
            f"PayPal request failed after retries ({last.status_code})", last.status_code, last.text[:500]
        )

    # ------------------------------------------------------------- money path

    def create_and_authorize(
        self,
        *,
        amount: Decimal,
        currency: str,
        buyer_email: str,
        seller_email: str,
        memo: str,
        request_id: str | None = None,
        vault_id: str | None = None,
        custom_id: str | None = None,
    ) -> EscrowOrder:
        """Create the order and authorize it: funds are HELD, not moved.

        `custom_id` (at most 127 characters) is stored on the order by PayPal and shown with it, so a PayPal order can
        be traced to the Trustee contract it pays for; Trustee sets it to `trustee:<contract sha256>`.

        With `vault_id` (a PayPal Vault payment token the payer consented to once, see `create_setup_token`) the order
        is created with `payment_source.paypal.vault_id`, carries no `experience_context`, and is authorized with no
        payer action: this is the human-not-present path. No approve link is ever produced for a vaulted order. PayPal
        may return the authorization inside the create response or only after `POST .../authorize`; both are handled.

        Real-sandbox caveat: an AUTHORIZE-intent order must be *approved by the
        payer* before `/authorize` succeeds (PayPal returns 422 ORDER_NOT_APPROVED
        otherwise). A headless agent therefore has two options, and this adapter
        supports both:

          * `requires_approval=True` (default for a live client) — the order
            carries a `payment_source.paypal.experience_context` and comes back
            `status="created"` with an `approve_url`. The payer approves in a
            browser, then `authorize_after_approval(order_id)` finishes the
            hold. This is the only path that works against real sandbox money.
          * `requires_approval=False` — single-shot create+authorize; correct
            only where the payer is pre-approved (vaulted payer, mock server).

        `dry_run` short-circuits both with local ids.
        """
        body: dict[str, Any] = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "amount": {"currency_code": currency, "value": str(amount)},
                    "payee": {"email_address": seller_email},
                    "description": memo,
                    **({"custom_id": custom_id[:127]} if custom_id else {}),
                }
            ],
        }
        if vault_id:
            body["payment_source"] = {"paypal": {"vault_id": vault_id}}
        elif self.requires_approval:
            body["payment_source"] = {
                "paypal": {
                    "experience_context": {
                        "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                        "brand_name": "Trustee escrow",
                        "locale": "en-US",
                        "landing_page": "LOGIN",
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "PAY_NOW",
                        "return_url": self.return_url,
                        "cancel_url": self.cancel_url,
                    }
                }
            }
        if self.dry_run:
            oid = f"dry-order-{uuid.uuid4().hex[:12]}"
            log.info("dry-run authorize %s -> %s", oid, amount)
            return EscrowOrder(
                order_id=oid,
                authorization_id=f"dry-authz-{uuid.uuid4().hex[:12]}",
                status="authorized",
                approve_url=None,
                details={
                    **({"funding": "vault"} if vault_id else {}),
                    **({"custom_id": custom_id} if custom_id else {}),
                },
            )

        resp = self._request_with_retry("POST", f"{self.base_url}/v2/checkout/orders", body, request_id=request_id)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"order create failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order = resp.json()
        oid = order["id"]

        if vault_id:
            authz_request_id = f"{request_id[: _REQUEST_ID_MAX - 6]}-authz" if request_id else None
            return self._vault_authorization(oid, order, authz_request_id)

        if self.requires_approval:
            # Funds stay un-held until the payer approves; hand the link back.
            approve = next(
                (lnk.get("href") for lnk in order.get("links", []) if lnk.get("rel") in ("approve", "payer-action")),
                None,
            )
            return EscrowOrder(
                order_id=oid,
                status="created",
                approve_url=approve,
                details={"buyer_email": buyer_email, "memo": memo},
            )

        # Single-shot path: the authorize step gets its own derived, stable id.
        authorize_request_id = f"{request_id[: _REQUEST_ID_MAX - 6]}-authz" if request_id else None
        return self.authorize_after_approval(oid, request_id=authorize_request_id)

    def _vault_authorization(self, order_id: str, created: dict[str, Any], request_id: str | None) -> EscrowOrder:
        """Finish a vaulted order: take the authorization from the create response, else authorize it now.

        Only the ids and statuses are kept in `details`: the raw responses name the payer's PayPal account and must
        not be stored or echoed."""
        authz = _first_authorization(created)
        if authz is None or not authz.get("id"):
            resp = self._request_with_retry(
                "POST",
                f"{self.base_url}/v2/checkout/orders/{order_id}/authorize",
                {"note": "trustee escrow hold"},
                request_id=request_id,
            )
            if resp.status_code not in (200, 201):
                raise PayPalError(f"authorize failed ({resp.status_code})", resp.status_code, resp.text[:500])
            authz = _first_authorization(resp.json())
        if authz is None or not authz.get("id"):
            raise PayPalError("PayPal returned no authorization for the vaulted order")
        return EscrowOrder(
            order_id=order_id,
            authorization_id=str(authz["id"]),
            status="authorized",
            details={"funding": "vault", "authorization_status": str(authz.get("status", ""))},
        )

    def authorize_after_approval(self, order_id: str, *, request_id: str | None = None) -> EscrowOrder:
        """Second half of the approval flow: hold the funds once the payer approved."""
        if self.dry_run:
            return EscrowOrder(
                order_id=order_id,
                authorization_id=f"dry-authz-{uuid.uuid4().hex[:12]}",
                status="authorized",
            )
        resp = self._request_with_retry(
            "POST",
            f"{self.base_url}/v2/checkout/orders/{order_id}/authorize",
            {"note": "trustee escrow hold"},
            request_id=request_id,
        )
        if resp.status_code not in (200, 201):
            raise PayPalError(f"authorize failed ({resp.status_code})", resp.status_code, resp.text[:500])
        authz = resp.json()
        authz_id = (authz.get("authorizations") or [{}])[0].get("id", "")
        return EscrowOrder(order_id=order_id, authorization_id=authz_id, status="authorized", details=authz)

    # ------------------------------------------------------------ vault (one-time consent)

    def create_setup_token(self, *, brand_name: str = "Trustee escrow") -> dict[str, Any]:
        """Start the one-time consent: PayPal returns a setup token and a link the payer approves once.

        `{"setup_token_id", "approve_url", "status"}`. The consent is for MERCHANT use with multiple payment tokens
        allowed, so later orders can name the saved payment method with no payer action."""
        if self.dry_run:
            sid = f"dry-setup-{uuid.uuid4().hex[:12]}"
            return {
                "setup_token_id": sid,
                "approve_url": f"{self.return_url}?approval_token_id={sid}",
                "status": "DRY_RUN",
            }
        body = {
            "payment_source": {
                "paypal": {
                    "usage_type": "MERCHANT",
                    "customer_type": "CONSUMER",
                    "permit_multiple_payment_tokens": True,
                    "experience_context": {
                        "brand_name": brand_name,
                        "return_url": self.vault_return_url,
                        "cancel_url": self.vault_cancel_url,
                        "shipping_preference": "NO_SHIPPING",
                    },
                }
            }
        }
        resp = self._request_with_retry("POST", f"{self.base_url}/v3/vault/setup-tokens", body)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"setup token create failed ({resp.status_code})", resp.status_code, resp.text[:500])
        data = resp.json()
        approve = next((lnk.get("href") for lnk in data.get("links", []) if lnk.get("rel") == "approve"), None)
        return {"setup_token_id": str(data["id"]), "approve_url": approve, "status": str(data.get("status", ""))}

    def get_setup_token(self, setup_token_id: str) -> dict[str, Any]:
        """`{"id", "status"}`; status is APPROVED once the payer consented. Dry-run: always APPROVED."""
        if self.dry_run:
            return {"id": setup_token_id, "status": "APPROVED"}
        resp = self._request_with_retry("GET", f"{self.base_url}/v3/vault/setup-tokens/{setup_token_id}")
        if resp.status_code != 200:
            raise PayPalError(f"get setup token failed ({resp.status_code})", resp.status_code, resp.text[:500])
        data = resp.json()
        return {"id": str(data.get("id", setup_token_id)), "status": str(data.get("status", ""))}

    def create_payment_token(self, setup_token_id: str) -> str:
        """Exchange an approved setup token for a payment token. Returns its id: the VAULT ID, a secret."""
        if self.dry_run:
            return f"dry-vault-{uuid.uuid4().hex[:12]}"
        body = {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}}
        resp = self._request_with_retry(
            "POST", f"{self.base_url}/v3/vault/payment-tokens", body, request_id=f"trustee-vault-{setup_token_id}"
        )
        if resp.status_code not in (200, 201):
            raise PayPalError(f"payment token create failed ({resp.status_code})", resp.status_code, resp.text[:500])
        return str(resp.json()["id"])

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Read an order, e.g. to poll whether the payer approved it (local dev gets no webhooks)."""
        if self.dry_run:
            return {"id": order_id, "status": "APPROVED"}
        resp = self._request_with_retry("GET", f"{self.base_url}/v2/checkout/orders/{order_id}")
        if resp.status_code != 200:
            raise PayPalError(f"get order failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order: dict[str, Any] = resp.json()
        return order

    def verify_webhook(self, headers: Mapping[str, str], body: dict[str, Any]) -> bool:
        """Ask PayPal whether a webhook delivery is genuine. True only on verification_status SUCCESS.

        Anyone can POST to our webhook URL, and these events move deal state, so an event that
        PayPal does not vouch for is dropped. Without a webhook id (live) nothing can be verified."""
        if self.dry_run:
            return self.allow_unverified_webhooks
        if not self.webhook_id:
            log.warning("webhook rejected: PAYPAL_WEBHOOK_ID is not configured")
            return False
        h = {k.lower(): v for k, v in headers.items()}
        fields = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
        }
        if not all(fields.values()):
            return False
        payload = {**fields, "webhook_id": self.webhook_id, "webhook_event": body}
        try:
            resp = self._request_with_retry(
                "POST", f"{self.base_url}/v1/notifications/verify-webhook-signature", payload
            )
        except PayPalError as exc:
            log.warning("webhook verification call failed: %s", exc)
            return False
        if resp.status_code != 200:
            log.warning("webhook verification call returned %s", resp.status_code)
            return False
        return bool(resp.json().get("verification_status") == "SUCCESS")

    def capture(
        self,
        order: EscrowOrder,
        *,
        amount: Decimal | None = None,
        currency: str = "USD",
        final_capture: bool = True,
        request_id: str | None = None,
    ) -> EscrowOrder:
        """Settle the held funds to the seller.

        For an AUTHORIZE-intent order the money lives on the *authorization*,
        so capture must go to `/v2/payments/authorizations/{id}/capture`.
        `/v2/checkout/orders/{id}/capture` is only valid for CAPTURE-intent
        orders and returns 422 here.

        `amount=None` captures everything held, with the same empty request as always. With an
        `amount`, only that much moves: the body carries the amount and `final_capture`. A final
        partial capture closes the authorization and PayPal releases the rest of the hold to the
        buyer; `final_capture=False` keeps it open for a further capture.
        """
        body: dict[str, Any] | None = None
        if amount is not None:
            value = amount.quantize(Decimal("0.01"))
            if value != amount or value <= 0:
                raise ValueError(f"capture amount must be a positive whole-cent value, got {amount}")
            body = {"amount": {"currency_code": currency, "value": str(value)}, "final_capture": final_capture}
        if self.dry_run:
            log.info("dry-run capture %s", order.order_id)
            order.status = "captured"
            order.details["capture_id"] = f"dry-cap-{uuid.uuid4().hex[:12]}"
            if body is not None:
                order.details["captured_amount"] = body["amount"]
                order.details["final_capture"] = final_capture
            return order
        if not order.authorization_id:
            raise PayPalError("capture requires an authorization_id (order was never authorized)")
        resp = self._request_with_retry(
            "POST",
            f"{self.base_url}/v2/payments/authorizations/{order.authorization_id}/capture",
            body,
            request_id=request_id,
        )
        if resp.status_code not in (200, 201):
            raise PayPalError(f"capture failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order.status = "captured"
        order.details = resp.json()
        return order

    def reauthorize(self, order: EscrowOrder, *, request_id: str | None = None) -> EscrowOrder:
        """Renew the hold after PayPal's 3-day honor period so the funds are checked again.

        POST `/v2/payments/authorizations/{id}/reauthorize` is allowed from day 4 until day 29 of the
        original authorization. PayPal answers with a NEW authorization id; every later capture and
        void must use it, so this swaps it into the order. The old id is kept in `details`.
        The 29-day validity still counts from the original authorization.
        """
        if self.dry_run:
            old = order.authorization_id
            order.authorization_id = f"dry-authz-{uuid.uuid4().hex[:12]}"
            order.details["previous_authorization_id"] = old
            order.details["reauthorizations"] = int(order.details.get("reauthorizations", 0)) + 1
            log.info("dry-run reauthorize %s: %s -> %s", order.order_id, old, order.authorization_id)
            return order
        if not order.authorization_id:
            raise PayPalError("reauthorize requires an authorization_id (order was never authorized)")
        resp = self._request_with_retry(
            "POST",
            f"{self.base_url}/v2/payments/authorizations/{order.authorization_id}/reauthorize",
            request_id=request_id,
        )
        if resp.status_code not in (200, 201):
            raise PayPalError(f"reauthorize failed ({resp.status_code})", resp.status_code, resp.text[:500])
        data = resp.json()
        new_id = str(data.get("id") or "")
        status = str(data.get("status") or "CREATED")
        if not new_id or status not in ("CREATED", "PENDING"):
            raise PayPalError(f"reauthorize returned no usable authorization (status {status})", resp.status_code)
        order.details["previous_authorization_id"] = order.authorization_id
        order.details["reauthorizations"] = int(order.details.get("reauthorizations", 0)) + 1
        order.authorization_id = new_id
        return order

    def void(self, order: EscrowOrder, reason: str = "", *, request_id: str | None = None) -> EscrowOrder:
        """Release the hold — nothing moved, buyer keeps their funds."""
        if self.dry_run:
            log.info("dry-run void %s (%s)", order.order_id, reason)
            order.status = "voided"
            order.details["void_reason"] = reason
            return order
        if not order.authorization_id:
            raise PayPalError("void requires an authorization_id (order was never authorized)")
        # The void endpoint takes no body; the reason belongs in our ledger.
        resp = self._request_with_retry(
            "POST",
            f"{self.base_url}/v2/payments/authorizations/{order.authorization_id}/void",
            request_id=request_id,
        )
        if resp.status_code not in (200, 201, 204):
            raise PayPalError(f"void failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order.status = "voided"
        # 204 No Content on success: there is no body to decode.
        order.details = resp.json() if resp.content else {}
        order.details["void_reason"] = reason
        return order

    def close(self) -> None:
        self._http.close()


def _first_authorization(order: dict[str, Any]) -> dict[str, Any] | None:
    """The first authorization in an Orders v2 response's `purchase_units[0].payments.authorizations`, if any."""
    for unit in order.get("purchase_units") or []:
        for authz in (unit.get("payments") or {}).get("authorizations") or []:
            if isinstance(authz, dict):
                return authz
    return None
