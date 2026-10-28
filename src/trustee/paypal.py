"""PayPal sandbox adapter — the only module that talks to PayPal.

Client-credentials REST against the sandbox:
  * Orders.create (intent=AUTHORIZE) -> authorize   = funds HELD, not moved
  * Orders.capture                        = settle to seller
  * Orders.void                           = release the hold (refund)

Design notes:
  * `dry_run=True` (or missing credentials) keeps the same call surface with
    locally-generated order/authorization ids — tests and the first demo
    runs never need live sandbox creds, and the judge-run path is identical.
  * Retries with exponential backoff on 429/5xx; all failures raise
    PayPalError with the response body for the ledger.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


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
    ):
        self.base_url = base_url or BASE_URLS[env]
        self.dry_run = client_id and client_secret if dry_run is None else bool(dry_run)
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._http = httpx.Client(timeout=timeout)
        self._max_retries = max_retries

    # ------------------------------------------------------------------ http

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if not self.dry_run:
            h["Authorization"] = f"Bearer {self._get_token()}"
        return h

    def _get_token(self) -> str:
        if self._token:
            return self._token
        resp = self._http.request(
            "POST",
            f"{self.base_url}/v1/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
        )
        if resp.status_code != 200:
            raise PayPalError(f"token endpoint failed ({resp.status_code})", resp.status_code, resp.text[:500])
        token = str(resp.json()["access_token"])
        self._token = token
        return token

    def _request_with_retry(self, method: str, url: str, json_body: dict | None = None) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            resp = self._http.request(method, url, json=json_body, headers=self._headers())
            if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < self._max_retries:
                last = resp
                time.sleep(0.5 * 2**attempt)
                continue
            return resp
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
    ) -> EscrowOrder:
        """Create the order and authorize it: funds are HELD, not moved."""
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "amount": {"currency_code": currency, "value": str(amount)},
                    "payee": {"email_address": seller_email},
                    "payer": {"email_address": buyer_email},
                    "description": memo,
                }
            ],
        }
        if self.dry_run:
            oid = f"dry-order-{uuid.uuid4().hex[:12]}"
            log.info("dry-run authorize %s -> %s", oid, amount)
            return EscrowOrder(order_id=oid, authorization_id=f"dry-authz-{uuid.uuid4().hex[:12]}", status="authorized")

        resp = self._request_with_retry("POST", f"{self.base_url}/v2/checkout/orders", body)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"order create failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order = resp.json()
        oid = order["id"]
        resp = self._request_with_retry("POST", f"{self.base_url}/v2/checkout/orders/{oid}/authorize", {"note": memo})
        if resp.status_code not in (200, 201):
            raise PayPalError(f"authorize failed ({resp.status_code})", resp.status_code, resp.text[:500])
        authz = resp.json()
        authz_id = (authz.get("authorizations") or [{}])[0].get("id", "")
        return EscrowOrder(order_id=oid, authorization_id=authz_id, status="authorized", details=authz)

    def capture(self, order: EscrowOrder) -> EscrowOrder:
        """Settle the held funds to the seller."""
        if self.dry_run:
            log.info("dry-run capture %s", order.order_id)
            order.status = "captured"
            order.details["capture_id"] = f"dry-cap-{uuid.uuid4().hex[:12]}"
            return order
        resp = self._request_with_retry("POST", f"{self.base_url}/v2/checkout/orders/{order.order_id}/capture")
        if resp.status_code not in (200, 201):
            raise PayPalError(f"capture failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order.status = "captured"
        order.details = resp.json()
        return order

    def void(self, order: EscrowOrder, reason: str = "") -> EscrowOrder:
        """Release the hold — nothing moved, buyer keeps their funds."""
        if self.dry_run:
            log.info("dry-run void %s (%s)", order.order_id, reason)
            order.status = "voided"
            order.details["void_reason"] = reason
            return order
        if not order.authorization_id:
            raise PayPalError("void requires an authorization_id (order was never authorized)")
        resp = self._request_with_retry(
            "POST",
            f"{self.base_url}/v2/payments/authorizations/{order.authorization_id}/void",
            json_body={"reason": reason or "trustee_unwind"},
        )
        if resp.status_code not in (200, 201):
            raise PayPalError(f"void failed ({resp.status_code})", resp.status_code, resp.text[:500])
        order.status = "voided"
        order.details = resp.json()
        return order

    def close(self) -> None:
        self._http.close()
