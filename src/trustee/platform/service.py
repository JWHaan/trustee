"""DealService — Trustee as a long-running service rather than a single in-process deal.

The Trustee agent owns one deal's money path. This class owns *many* of them: it keeps the
identity registries, the mandate engine and the deadline sweeper, persists every deal after
every call, and rebuilds all of it from the store on start. That last part is the point: if the
process dies while PayPal holds a buyer's funds, the next process finds the deal in the store,
re-reserves its budget and keeps sweeping it, so the hold is eventually captured or voided.

Everything here is blocking (PayPal HTTP, SQLite). The HTTP layer calls it via `asyncio.to_thread`.
Errors the API must surface are `ServiceError(status, detail, **extra)`; anything else is a bug.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..agents.arbiter import Judge
from ..agents.trustee import Trustee, TrusteeDecision, is_transient_paypal
from ..evidence import build_evidence_pack
from ..funding import FundingError, FundingRegistry
from ..identity import AgentKey, AgentRecord, AgentRegistry, PrincipalRegistry, verify
from ..intent import Autonomy, IntentBook, IntentMandate
from ..ledger import Ledger
from ..llm import LLM
from ..mandate import Mandate, MandateEngine, MandateReplay
from ..paypal import PayPalClient
from ..protocol import Attest, Contract, Delivery
from ..receipts import build_receipt, load_signing_key
from ..reputation import LocalReputation, ReputationProvider
from ..state import Deal, DealState, IllegalTransition
from ..store import Store, StoreConflict
from ..summary import summarize
from ..sweeper import DeadlineSweeper
from ..verification import BlobError, BlobStore, DeliveryVerifier
from .wire import (
    ID_PATTERN,
    agent_signing_bytes,
    download_signing_bytes,
    funding_revoke_signing_bytes,
    funding_setup_signing_bytes,
    intent_revoke_signing_bytes,
    mandate_from_dict,
    mandate_to_dict,
    principal_signing_bytes,
    revoke_signing_bytes,
)

log = logging.getLogger(__name__)

MAX_APPROVAL_POLLS = 20  # PayPal reads per sweep; a long backlog is worked off across sweeps

_ID = re.compile(ID_PATTERN)


class ServiceError(Exception):
    """An error with an HTTP meaning. `extra` is merged into the response body."""

    def __init__(self, status: int, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


class DealService:
    def __init__(
        self,
        store: Store,
        paypal: PayPalClient,
        ledger: Ledger,
        llm: LLM | None = None,
        judge_factory: Callable[[], Judge] | None = None,
        sweeper: DeadlineSweeper | None = None,
        operator_token: str | None = None,
        signing_key: AgentKey | None = None,
        reputation: ReputationProvider | None = None,
        blobs: BlobStore | None = None,
    ):
        self.store = store
        # Delivered files the sellers hand to Trustee, and the checks Trustee runs on them itself.
        self.blobs = blobs if blobs is not None else BlobStore()
        self.delivery_verifier = DeliveryVerifier(self.blobs)
        self.paypal = paypal
        self.ledger = ledger
        self.llm = llm
        self.judge_factory = judge_factory
        self.sweeper = sweeper if sweeper is not None else DeadlineSweeper()
        self.operator_token = operator_token or None
        self.registry = AgentRegistry()
        self.principals = PrincipalRegistry()
        # Counterparty reputation rules in mandates read this; the local store is the default source.
        self.reputation: ReputationProvider = reputation if reputation is not None else LocalReputation(store)
        self.mandates = MandateEngine(principals=self.principals, reputation=self.reputation)
        # Human-not-present payments: intent mandates, the vaulted funding sources they name, and Trustee's own key.
        self.funding = FundingRegistry(store, paypal)
        self.intents = IntentBook()
        self.autonomy = Autonomy(self.intents, self.funding, self.principals)
        self.signing_key = signing_key if signing_key is not None else load_signing_key(store)
        self._deals: dict[str, tuple[Trustee, Contract]] = {}
        self._lock = threading.RLock()
        self._deal_locks: dict[str, threading.RLock] = {}
        self._by_order: dict[str, str] = {}  # PayPal order id -> deal id (webhooks and return URLs)
        self._by_authz: dict[str, str] = {}  # PayPal authorization id -> deal id
        self._poll_cursor = 0
        self._revoked: set[str] = set()  # sticky: a revoked agent id never gets a mandate again
        self._unpersisted: set[str] = set()  # deals whose latest state could not be written; retried each sweep
        self.sweeper.deal_lock = self._deal_lock
        store.attach(ledger)  # the tape is durable
        self.rehydrate()

    # ------------------------------------------------------------- lifecycle

    def rehydrate(self) -> None:
        """Rebuild registries, mandates, budgets and open deals from the store."""
        for row in self.store.list_agents():
            self._ensure_principal(row["principal"])
            self.registry.register(
                AgentRecord(
                    agent_id=row["agent_id"],
                    public_key=row["public_key"],
                    principal=row["principal"],
                    roles=frozenset(row["roles"]),
                    paypal_email=row["paypal_email"],
                    metadata=row["metadata"] or {},
                )
            )
            stored = self.store.get_mandate(row["agent_id"])
            if stored is None:
                continue
            self._ensure_principal(stored["principal"])
            self.mandates.grant(mandate_from_dict(stored["body"]))
            if stored["revoked"]:
                self.mandates.revoke(row["agent_id"])
                self._revoked.add(row["agent_id"])
        for row in self.store.list_intents():
            try:
                intent = IntentMandate.model_validate(row["body"])
            except ValueError:
                log.warning("stored intent for %s is unreadable; skipped", row["agent_id"])
                continue
            self._ensure_principal(intent.principal)
            self.intents.put(intent, revoked=row["revoked"])
        # Captured deals are spend: without this a restart would hand every budget back.
        for row in self.store.list_deals(states=[DealState.SETTLED.value], limit=1_000_000):
            if row["contract"] is not None:
                settlement = (row["snapshot"] or {}).get("settlement")
                captured = Decimal(settlement["captured"]) if settlement else None
                self.mandates.commit(Contract.model_validate(row["contract"]), captured)
        for row in self.store.open_deals():
            self._load_row(row)

    def _load_row(self, row: dict[str, Any]) -> tuple[Trustee, Contract] | None:
        if row["contract"] is None:
            return None
        contract = Contract.model_validate(row["contract"])
        trustee = self._new_trustee(contract.deal_id, row["snapshot"])
        entry = (trustee, contract)
        with self._lock:
            self._deals[contract.deal_id] = entry
        self._index(trustee, contract)
        if not trustee.deal.is_terminal:
            if (row["snapshot"].get("escrow") or {}).get("status") == "authorized":
                self.mandates.reserve(contract)
                authority = row["snapshot"].get("autonomy") or {}
                if authority and not authority.get("human_present", True):
                    self.intents.note_purchase(contract.deal_id, contract.buyer, _parse_when(authority.get("at")))
            self.sweeper.register(trustee, contract)
        return entry

    def _new_trustee(self, deal_id: str, snapshot: dict[str, Any] | None = None) -> Trustee:
        trustee = self._build_trustee(deal_id, snapshot)
        trustee.before_money = lambda contract: self._write_ahead(trustee, contract)
        trustee.verifier = self.delivery_verifier.verify
        return trustee

    def _write_ahead(self, trustee: Trustee, contract: Contract) -> None:
        """Make a deal's money intent durable before its PayPal call; raise if that is not possible.

        Runs inside a Trustee call, which the service only makes while holding the deal lock, so taking that
        (re-entrant) lock here keeps the order deal lock -> Trustee lock everywhere. The ledger backlog is written
        first: money does not move on top of events that are not on disk."""
        with self._deal_lock(contract.deal_id):
            self.store.flush_backlog()
            snap = trustee.snapshot()
            self.store.upsert_deal(contract.deal_id, snap["state"], contract.model_dump(mode="json"), snap)

    def _build_trustee(self, deal_id: str, snapshot: dict[str, Any] | None = None) -> Trustee:
        judge = self.judge_factory() if self.judge_factory is not None else None
        recorder = self._state_recorder(deal_id)
        if snapshot is not None:
            return Trustee.from_snapshot(
                snapshot,
                mandates=self.mandates,
                paypal=self.paypal,
                llm=self.llm,
                ledger=self.ledger,
                judge=judge,
                registry=self.registry,
                autonomy=self.autonomy,
                on_transition=recorder,
            )
        return Trustee(
            Deal(deal_id, on_transition=recorder),
            self.mandates,
            self.paypal,
            llm=self.llm,
            ledger=self.ledger,
            judge=judge,
            registry=self.registry,
            autonomy=self.autonomy,
        )

    def _state_recorder(self, deal_id: str) -> Callable[[str, str, str], None]:
        def record(reason: str, frm: str, to: str) -> None:
            self.ledger.append(
                deal_id, "trustee", "state", event="transition", state_from=frm, state_to=to, note=reason
            )

        return record

    # ------------------------------------------------------------------ auth

    def check_registration_token(self, authorization: str | None) -> None:
        """Open registration unless an operator token is configured (dev/hackathon default)."""
        if self.operator_token is None:
            return
        if not self._bearer_matches(authorization):
            raise ServiceError(401, "registration requires the operator bearer token")

    def require_operator(self, authorization: str | None) -> None:
        if self.operator_token is None:
            raise ServiceError(403, "operator endpoints are disabled: no TRUSTEE_OPERATOR_TOKEN is configured")
        if not self._bearer_matches(authorization):
            raise ServiceError(401, "operator bearer token required")

    def _bearer_matches(self, authorization: str | None) -> bool:
        if not authorization or not self.operator_token:
            return False
        scheme, _, token = authorization.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), self.operator_token)

    # ------------------------------------------------------------- identities

    def _ensure_principal(self, principal: str) -> str | None:
        """Principal key from memory, falling back to (and caching from) the store."""
        key = self.principals.get(principal)
        if key is None:
            row = self.store.get_principal(principal)
            if row is not None:
                key = str(row["public_key"])
                self.principals.register(principal, key)
        return key

    def register_principal(self, principal: str, public_key: str, proof: str) -> tuple[dict[str, Any], bool]:
        if not verify(public_key, proof, principal_signing_bytes(principal, public_key)):
            raise ServiceError(401, "invalid proof of possession for principal key")
        with self._lock:
            existing = self._ensure_principal(principal)
            if existing is not None and existing != public_key:
                raise ServiceError(409, f"principal {principal!r} is already registered with a different key")
            if existing is not None:
                return {"principal": principal, "public_key": public_key}, False
            try:
                self.store.put_principal(principal, public_key)
            except StoreConflict as exc:
                raise ServiceError(409, str(exc)) from None
            self.principals.register(principal, public_key)
        return {"principal": principal, "public_key": public_key}, True

    def register_agent(
        self,
        agent_id: str,
        public_key: str,
        principal: str,
        roles: list[str],
        paypal_email: str | None,
        proof: str,
        principal_signature: str,
    ) -> tuple[dict[str, Any], bool]:
        body = agent_signing_bytes(agent_id, public_key, principal, roles, paypal_email)
        if not verify(public_key, proof, body):
            raise ServiceError(401, "invalid proof of possession for agent key")
        with self._lock:
            if self._ensure_principal(principal) is None:
                raise ServiceError(422, f"unknown principal {principal!r}: register it first")
            if not self.principals.verify(principal, principal_signature, body):
                raise ServiceError(401, f"principal {principal!r} did not vouch for this agent")
            existing = self.registry.get(agent_id)
            if existing is not None and existing.public_key != public_key:
                raise ServiceError(409, f"agent {agent_id!r} is already registered with a different key")
            if existing is not None and existing.principal != principal:
                raise ServiceError(409, f"agent {agent_id!r} belongs to a different principal")
            record = AgentRecord(
                agent_id=agent_id,
                public_key=public_key,
                principal=principal,
                roles=frozenset(roles),
                paypal_email=paypal_email,
            )
            try:
                self.store.put_agent(agent_id, public_key, principal, sorted(record.roles), paypal_email)
            except StoreConflict as exc:
                raise ServiceError(409, str(exc)) from None
            self.registry.register(record)
        return self._public_agent(record), existing is None

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        record = self.registry.get(agent_id)
        if record is None:
            raise ServiceError(404, f"unknown agent {agent_id!r}")
        return self._public_agent(record)

    @staticmethod
    def _public_agent(record: AgentRecord) -> dict[str, Any]:
        # No secrets live here, but the payout address is the owner's business: expose only that one is set.
        return {
            "agent_id": record.agent_id,
            "public_key": record.public_key,
            "principal": record.principal,
            "roles": sorted(record.roles),
            "has_paypal_email": bool(record.paypal_email),
        }

    # --------------------------------------------------------------- mandates

    def publish_mandate(self, mandate: Mandate) -> dict[str, Any]:
        record = self.registry.get(mandate.agent_id)
        if record is None:
            raise ServiceError(404, f"unknown agent {mandate.agent_id!r}")
        if record.principal != mandate.principal:
            raise ServiceError(403, f"agent {mandate.agent_id!r} belongs to principal {record.principal!r}")
        if self._ensure_principal(mandate.principal) is None or not self.principals.verify(
            mandate.principal, mandate.principal_signature, mandate.signing_bytes()
        ):
            raise ServiceError(401, f"mandate is not signed by principal {mandate.principal!r}")
        with self._lock:
            if mandate.agent_id in self._revoked:
                raise ServiceError(409, f"mandate for {mandate.agent_id!r} was revoked; register a new agent id")
            try:
                self.mandates.check_grant(mandate)
            except MandateReplay as exc:
                raise ServiceError(409, str(exc)) from None
            self.store.put_mandate(mandate.agent_id, mandate.principal, mandate_to_dict(mandate))
            self.mandates.grant(mandate)
        return {"agent_id": mandate.agent_id, "principal": mandate.principal, "grants": mandate.grants}

    def revoke_mandate(self, agent_id: str, signature: str) -> dict[str, Any]:
        record = self.registry.get(agent_id)
        if record is None:
            raise ServiceError(404, f"unknown agent {agent_id!r}")
        if self._ensure_principal(record.principal) is None or not self.principals.verify(
            record.principal, signature, revoke_signing_bytes(agent_id)
        ):
            raise ServiceError(401, f"revocation is not signed by principal {record.principal!r}")
        with self._lock:
            if not self.store.revoke_mandate(agent_id):
                raise ServiceError(404, f"no mandate on file for {agent_id!r}")
            self.mandates.revoke(agent_id)
            self._revoked.add(agent_id)
        return {"agent_id": agent_id, "revoked": True}

    # ------------------------------------------------------- intents and funding

    def _require_principal_signed(self, principal: str, signature: str, body: bytes, what: str) -> None:
        if self._ensure_principal(principal) is None:
            raise ServiceError(404, f"unknown principal {principal!r}: register it first")
        if not self.principals.verify(principal, signature, body):
            raise ServiceError(401, f"{what} is not signed by principal {principal!r}")

    def publish_intent(self, intent: IntentMandate) -> dict[str, Any]:
        """Store a principal-signed intent mandate for one buyer agent. The signature is the authorization."""
        record = self.registry.get(intent.agent_id)
        if record is None:
            raise ServiceError(404, f"unknown agent {intent.agent_id!r}")
        if record.principal != intent.principal:
            raise ServiceError(403, f"agent {intent.agent_id!r} belongs to principal {record.principal!r}")
        if self._ensure_principal(intent.principal) is None or not self.principals.verify(
            intent.principal, intent.principal_signature, intent.signing_bytes()
        ):
            raise ServiceError(401, f"intent mandate is not signed by principal {intent.principal!r}")
        if not intent.require_escrow or intent.human_present:
            raise ServiceError(422, "an intent mandate must have require_escrow=true and human_present=false")
        source = self.store.funding_public(intent.funding_ref)
        if source is None or source["principal"] != intent.principal or source["revoked"]:
            raise ServiceError(422, "funding_ref is not an active funding source of this principal")
        digest = intent.digest()
        with self._lock:
            if self.intents.digest_revoked(digest):
                raise ServiceError(409, "this exact intent mandate was revoked; sign a new one")
            current = self.intents.get(intent.agent_id)
            created = current is None or current.digest() != digest
            if current is not None and created and intent.issued_at <= current.issued_at:
                raise ServiceError(409, "a newer intent mandate is already on file (replay of an older one)")
            if created:
                self.store.put_intent(intent.agent_id, intent.principal, intent.public(), digest)
                self.intents.put(intent)
        return {
            "agent_id": intent.agent_id,
            "principal": intent.principal,
            "intent_mandate_digest": digest,
            "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
            "created": created,
        }

    def revoke_intent(self, agent_id: str, signature: str) -> dict[str, Any]:
        record = self.registry.get(agent_id)
        if record is None:
            raise ServiceError(404, f"unknown agent {agent_id!r}")
        self._require_principal_signed(
            record.principal, signature, intent_revoke_signing_bytes(agent_id), "the revocation"
        )
        with self._lock:
            if self.intents.get(agent_id) is None or not self.store.revoke_intent(agent_id):
                raise ServiceError(404, f"no intent mandate on file for {agent_id!r}")
            self.intents.revoke(agent_id)
        return {"agent_id": agent_id, "revoked": True}

    def begin_funding(
        self, principal: str, nonce: str, signature: str, funding_ref: str | None = None
    ) -> dict[str, Any]:
        """Start the one-time PayPal consent for `principal`. In dry-run there is no payer, so it completes at once."""
        self._require_principal_signed(
            principal, signature, funding_setup_signing_bytes(principal, nonce, funding_ref), "the funding request"
        )
        try:
            started = self.funding.begin(principal, funding_ref)
            if started.get("status") == "active":
                return started
            if self.paypal.dry_run:
                info = self.funding.complete(self._setup_token_of(started["funding_ref"]))
                return {**info, "approve_url": None}
        except FundingError as exc:
            raise ServiceError(exc.status, exc.detail) from None
        return started

    def _setup_token_of(self, funding_ref: str) -> str:
        info = self.store.funding_public(funding_ref)
        assert info is not None and info["setup_token_id"]
        return str(info["setup_token_id"])

    def funding_return(self, setup_token_id: str) -> dict[str, Any]:
        """The payer came back from the one-time consent. Vaults the payment method; nothing secret is returned."""
        try:
            return self.funding.complete(setup_token_id)
        except FundingError as exc:
            raise ServiceError(exc.status, exc.detail) from None

    def revoke_funding(self, principal: str, funding_ref: str, signature: str) -> dict[str, Any]:
        self._require_principal_signed(
            principal, signature, funding_revoke_signing_bytes(principal, funding_ref), "the revocation"
        )
        try:
            return self.funding.revoke(principal, funding_ref)
        except FundingError as exc:
            raise ServiceError(exc.status, exc.detail) from None

    def funding_status(self, funding_ref: str) -> dict[str, Any]:
        try:
            return self.funding.status(funding_ref)
        except FundingError as exc:
            raise ServiceError(exc.status, exc.detail) from None

    # --------------------------------------------------------------- receipts

    def trustee_key(self) -> dict[str, Any]:
        return {"algorithm": "Ed25519", "public_key": self.signing_key.public_key_b64, "encoding": "urlsafe-base64"}

    def receipt(self, deal_id: str) -> dict[str, Any]:
        """The signed settlement receipt of a finished deal (issued on first request if a restart missed it)."""
        trustee, contract = self._require(deal_id)
        if not trustee.deal.is_terminal:
            raise ServiceError(
                409, f"deal {deal_id} is {trustee.deal.state.value}: no receipt until it is settled or voided"
            )
        existing = self.store.get_receipt(deal_id)
        if existing is not None:
            return existing
        return self._issue_receipt(trustee, contract) or self._missing_receipt(deal_id)

    def evidence(self, deal_id: str) -> dict[str, Any]:
        """The signed dispute evidence pack for a deal (trustee.evidence): available at any state."""
        trustee, contract = self._require(deal_id)
        receipt = self.store.get_receipt(deal_id) if trustee.deal.is_terminal else None
        events = self.store.events(deal_id, limit=100_000)
        return build_evidence_pack(
            contract=contract, snapshot=trustee.snapshot(), events=events, receipt=receipt, key=self.signing_key
        )

    def _missing_receipt(self, deal_id: str) -> dict[str, Any]:
        raise ServiceError(500, f"could not issue a receipt for {deal_id}")

    def _issue_receipt(self, trustee: Trustee, contract: Contract) -> dict[str, Any] | None:
        with self._deal_lock(contract.deal_id):
            existing = self.store.get_receipt(contract.deal_id)
            if existing is not None:
                return existing
            events = self.store.events(contract.deal_id, limit=1_000_000)
            head = events[-1]["hash"] if events else ""
            receipt = build_receipt(
                trustee.snapshot(), contract, key=self.signing_key, ledger_head=head, ledger_events=len(events)
            )
            if not self.store.put_receipt(contract.deal_id, receipt):
                return self.store.get_receipt(contract.deal_id)
            self.ledger.append(
                contract.deal_id,
                "trustee",
                "receipt",
                event="receipt_issued",
                outcome=receipt["outcome"],
                human_present=receipt["human_present"],
                note=f"signed {receipt['outcome']} receipt issued; ledger head {head[:12]}",
            )
            return receipt

    # ------------------------------------------------------------------ deals

    def _deal_lock(self, deal_id: str) -> threading.RLock:
        with self._lock:
            return self._deal_locks.setdefault(deal_id, threading.RLock())

    def _entry(self, deal_id: str) -> tuple[Trustee, Contract] | None:
        with self._lock:
            entry = self._deals.get(deal_id)
        if entry is not None:
            return entry
        row = self.store.get_deal(deal_id)  # e.g. a terminal deal from before the last restart
        return self._load_row(row) if row is not None else None

    def _require(self, deal_id: str) -> tuple[Trustee, Contract]:
        entry = self._entry(deal_id)
        if entry is None:
            raise ServiceError(404, f"unknown deal {deal_id!r}")
        return entry

    def _persist(self, trustee: Trustee, contract: Contract) -> None:
        """Write the deal's state. Runs under the deal lock so snapshots land in order.
        A storage failure is not raised: the money has already moved, and failing the request would not undo it.
        The deal is remembered and written again on every sweep until it succeeds (see `durability`)."""
        with self._deal_lock(contract.deal_id):
            try:
                snap = trustee.snapshot()
                self._index(trustee, contract, snap)
                self.store.upsert_deal(contract.deal_id, snap["state"], contract.model_dump(mode="json"), snap)
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._unpersisted.add(contract.deal_id)
                log.exception("CRITICAL: could not persist deal %s; retrying on every sweep", contract.deal_id)
                return
            with self._lock:
                self._unpersisted.discard(contract.deal_id)
            if trustee.deal.is_terminal:
                try:
                    self._issue_receipt(trustee, contract)
                except Exception:  # noqa: BLE001 - the receipt is evidence, not the money path
                    log.exception("could not issue a receipt for %s", contract.deal_id)

    def _index(self, trustee: Trustee, contract: Contract, snap: dict[str, Any] | None = None) -> None:
        escrow = (snap or trustee.snapshot())["escrow"]
        if escrow is None:
            return
        with self._lock:
            if escrow["order_id"]:
                self._by_order[escrow["order_id"]] = contract.deal_id
            if escrow["authorization_id"]:
                self._by_authz[escrow["authorization_id"]] = contract.deal_id

    def _find(
        self, *, order_id: str | None = None, authorization_id: str | None = None
    ) -> tuple[Trustee, Contract] | None:
        """Deal for a PayPal id. The index covers open deals; a miss scans the store for older (terminal) ones."""
        for index, key in ((self._by_authz, authorization_id), (self._by_order, order_id)):
            if key:
                with self._lock:
                    deal_id = index.get(key)
                if deal_id is not None:
                    return self._entry(deal_id)
        wanted = {k for k in (order_id, authorization_id) if k}
        if not wanted:
            return None
        for row in self.store.list_deals(limit=100_000):
            esc = (row["snapshot"] or {}).get("escrow") or {}
            if wanted & {esc.get("order_id"), esc.get("authorization_id")}:
                return self._entry(row["deal_id"])
        return None

    def view(self, deal_id: str) -> dict[str, Any]:
        trustee, contract = self._require(deal_id)
        return self._view(trustee, contract)

    def record(self, deal_id: str) -> dict[str, Any]:
        """Live facts about one deal for protocol bindings other than REST (the A2A task view).

        Read-only. `snapshot` is the same dict the store persists, taken under the trustee's lock, so a
        reader never sees a half-applied transition. `updated_at` is the store's last write time."""
        trustee, contract = self._require(deal_id)
        row = self.store.get_deal(deal_id)
        return {
            "deal_id": deal_id,
            "snapshot": trustee.snapshot(),
            "contract": contract.model_dump(mode="json"),
            "updated_at": row["updated_at"] if row else None,
            "receipt": self.store.get_receipt(deal_id) if trustee.deal.is_terminal else None,
        }

    @staticmethod
    def _view(trustee: Trustee, contract: Contract) -> dict[str, Any]:
        snap = trustee.snapshot()
        escrow = snap["escrow"]
        ruling = snap["ruling"]
        view: dict[str, Any] = {
            "deal_id": contract.deal_id,
            "state": snap["state"],
            "contract": contract.key_clauses(),
            "escrow": None
            if escrow is None
            else {
                "order_id": escrow["order_id"],
                "authorization_id": escrow["authorization_id"],
                "status": escrow["status"],
                "approve_url": escrow["approve_url"],
            },
            "ruling": None
            if ruling is None
            else {
                "decision": ruling["decision"],
                "clause": ruling["clause"],
                "reasoning": ruling["reasoning"],
                "objective": ruling["objective"],
                "llm_assisted": ruling["llm_assisted"],
            },
            "dispute_deadline": snap["dispute_deadline"],
            "needs_attention": snap.get("needs_attention"),
            "history": snap["history"],
            "authority": _authority_view(snap.get("autonomy")),
        }
        if snap.get("settlement"):  # pro-rata settlement: what was captured and what went back to the buyer
            view["settlement"] = snap["settlement"]
        if snap.get("verification"):  # Trustee's own check of the delivered file
            view["verification"] = snap["verification"]
        # What happened, in plain words: the same summary the arena shows people.
        view["summary"] = summarize(
            state=snap["state"],
            price=contract.price,
            currency=contract.currency,
            escrow_status=escrow["status"] if escrow else None,
            ruling=view["ruling"],
            settlement=snap.get("settlement"),
            needs_attention=snap.get("needs_attention"),
            dispute_deadline=snap["dispute_deadline"],
            human_present=view["authority"]["human_present"],
            authority_outcome=view["authority"]["outcome"],
            verification=snap.get("verification"),
        )
        return view

    def events(self, deal_id: str, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        self._require(deal_id)
        rows = self.store.events(deal_id, after_seq=max(0, after_seq), limit=max(1, min(limit, 1000)))
        return {"deal_id": deal_id, "events": rows, "next_after_seq": rows[-1]["seq"] if rows else after_seq}

    def durability(self) -> dict[str, int]:
        """What is not yet on disk: ledger events and deal states waiting for a retry. Both should be 0."""
        with self._lock:
            deals = len(self._unpersisted)
        return {"events_waiting": self.store.backlog_size, "deals_waiting": deals}

    def _raise_for(self, decision: TrusteeDecision, trustee: Trustee, contract: Contract, subject: str) -> None:
        if decision.action == "rejected_identity":
            raise ServiceError(401, decision.explanation or "identity check failed")
        if decision.action == "rejected_mandate":
            violations = decision.report.violations if decision.report is not None else [decision.explanation]
            raise ServiceError(403, "mandate check failed", violations=violations)
        if decision.action == "unavailable":
            # Storage could not record the intent, so nothing was sent to PayPal. Retrying is safe.
            raise ServiceError(503, decision.explanation, deal=self._view(trustee, contract))
        if decision.action == "error":
            if subject == "delivery":
                raise ServiceError(422, "delivery does not match the contract (deliverable_ref)")
            # PayPal failed. Funds are exactly where the view says; retrying the same call is safe.
            raise ServiceError(502, decision.explanation or "PayPal call failed", deal=self._view(trustee, contract))

    def open_deal(self, contract: Contract) -> tuple[dict[str, Any], bool]:
        """Form a deal from a doubly signed contract. Returns (view, created)."""
        if not _ID.fullmatch(contract.deal_id):
            raise ServiceError(422, "deal_id must match " + ID_PATTERN)
        with self._deal_lock(contract.deal_id):
            entry = self._entry(contract.deal_id)
            created = entry is None
            if entry is not None:
                trustee, stored = entry
                if stored.digest() != contract.digest():
                    raise ServiceError(409, f"deal {contract.deal_id!r} already exists with different terms")
                retry = trustee.deal.state == DealState.CONTRACTED and trustee.snapshot()["escrow"] is None
                if not retry:
                    return self._view(trustee, stored), False
                contract = stored  # authorize failed earlier; the PayPal request id is deterministic, so retry is safe
            else:
                trustee = self._new_trustee(contract.deal_id)
                trustee.deal.transition(DealState.NEGOTIATING, "signed contract submitted")
            if trustee.pending_money is not None and trustee.pending_money.get("op") == "authorize-create":
                # The escrow opening was decided and recorded before; finish that call, do not decide again.
                decision = trustee.resume_pending(contract)
            else:
                decision = trustee.on_contract(contract)
            if decision.action in ("rejected_identity", "rejected_mandate"):
                # Never stored: a refused contract must not squat on the deal id or touch PayPal.
                self._raise_for(decision, trustee, contract, "contract")
            with self._lock:
                self._deals[contract.deal_id] = (trustee, contract)
            self._persist(trustee, contract)
            self.sweeper.register(trustee, contract)
            self._raise_for(decision, trustee, contract, "contract")
            return self._view(trustee, contract), created

    def on_delivery(self, deal_id: str, delivery: Delivery) -> dict[str, Any]:
        trustee, contract = self._require(deal_id)
        with self._deal_lock(deal_id):
            try:
                decision = trustee.on_delivery(delivery, contract)
            except IllegalTransition as exc:
                raise ServiceError(409, str(exc)) from None
            self._persist(trustee, contract)
            self._raise_for(decision, trustee, contract, "delivery")
            return self._view(trustee, contract)

    # ------------------------------------------------------- the delivered file

    def upload_deliverable(self, deal_id: str, data: bytes) -> dict[str, Any]:
        """The seller hands Trustee the file before recording the delivery, so Trustee can check it itself.

        No signature is needed: the file is stored under its own sha256, and Trustee only checks the bytes under the
        hash the seller *signs* in the delivery. Uploading anything else changes nothing."""
        trustee, _ = self._require(deal_id)
        if trustee.deal.state not in (DealState.ESCROWED, DealState.DELIVERED):
            raise ServiceError(409, f"files are accepted while funds are held; the deal is {trustee.deal.state.value}")
        try:
            digest = self.blobs.put(data, deal_id)
        except BlobError as exc:
            raise ServiceError(413 if "larger" in str(exc) else 422, str(exc)) from None
        return {"deal_id": deal_id, "sha256": digest, "size": len(data)}

    def download_deliverable(self, deal_id: str, signature: str | None) -> tuple[bytes, str]:
        """The buyer gets the file from Trustee once the payment has settled: payment and file change hands together.

        Signed by the buyer's agent key over `download_signing_bytes(deal_id)`."""
        trustee, contract = self._require(deal_id)
        if not self.registry.verify(contract.buyer, signature, download_signing_bytes(deal_id)):
            raise ServiceError(401, "the download must be signed by the buyer's agent key")
        if trustee.deal.state != DealState.SETTLED:
            raise ServiceError(409, "the file is released to the buyer when the payment settles")
        record = trustee.snapshot().get("verification") or {}
        data = self.blobs.get(str(record.get("sha256_received") or "")) if record else None
        if data is None:
            raise ServiceError(404, "Trustee holds no file for this deal; the seller delivered it directly")
        return data, contract.good.deliverable_ref

    def on_attest(self, deal_id: str, attest: Attest) -> dict[str, Any]:
        trustee, contract = self._require(deal_id)
        with self._deal_lock(deal_id):
            # Capturing before delivery would move money and then fail the state transition.
            if trustee.deal.state not in (DealState.DELIVERED, DealState.DISPUTED) and not trustee.deal.is_terminal:
                raise ServiceError(
                    409, f"cannot attest a deal in state {trustee.deal.state.value}; delivery comes first"
                )
            try:
                decision = trustee.on_attest(attest, contract)
            except IllegalTransition as exc:
                raise ServiceError(409, str(exc)) from None
            self._persist(trustee, contract)
            self._raise_for(decision, trustee, contract, "attest")
            return self._view(trustee, contract)

    def resolve(
        self, deal_id: str, decision: str, operator: str, reason: str, authorization: str | None
    ) -> dict[str, Any]:
        self.require_operator(authorization)
        trustee, contract = self._require(deal_id)
        with self._deal_lock(deal_id):
            try:
                result = trustee.resolve_dispute(contract, decision, operator, reason)  # type: ignore[arg-type]
            except IllegalTransition as exc:
                raise ServiceError(409, str(exc)) from None
            except ValueError as exc:
                raise ServiceError(422, str(exc)) from None
            self._persist(trustee, contract)
            self._raise_for(result, trustee, contract, "resolve")
            return self._view(trustee, contract)

    # ------------------------------------------------------- payer approval

    def approval_return(self, order_id: str) -> dict[str, Any]:
        """The payer came back from PayPal approved: hold the funds. Replays are no-ops."""
        return self._approval(order_id, cancelled=False)

    def approval_cancel(self, order_id: str) -> dict[str, Any]:
        return self._approval(order_id, cancelled=True)

    def _approval(self, order_id: str, *, cancelled: bool) -> dict[str, Any]:
        entry = self._find(order_id=order_id)
        if entry is None:
            raise ServiceError(404, f"unknown PayPal order {order_id!r}")
        trustee, contract = entry
        with self._deal_lock(contract.deal_id):
            decision = trustee.on_approval_cancelled(contract) if cancelled else trustee.on_approved(contract)
            self._persist(trustee, contract)
            return {
                "decision": decision.action,
                "explanation": decision.explanation,
                "deal": self._view(trustee, contract),
            }

    # ---------------------------------------------------------------- webhooks

    def handle_webhook(self, headers: Mapping[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """PayPal's events are ground truth for money movement. Returns (http status, body).

        Unverified -> 400 and nothing recorded. Verified events always answer 200 so PayPal stops
        retrying; an unexpected crash while handling is the one exception (500, and the delivery is
        un-recorded so the retry is processed rather than deduped)."""
        if not self.paypal.verify_webhook(headers, body):
            return 400, {"detail": "webhook signature verification failed"}
        event_id = str(body.get("id") or "")
        event_type = str(body.get("event_type") or "")
        raw_resource = body.get("resource")
        resource: dict[str, Any] = raw_resource if isinstance(raw_resource, dict) else {}
        if not event_id or not event_type:
            return 400, {"detail": "webhook event needs an id and an event_type"}
        if not self.store.record_webhook(event_id, event_type, resource.get("id"), body):
            return 200, {"duplicate": True}
        try:
            status, result = self._apply_webhook(event_type, resource)
        except Exception:  # noqa: BLE001
            log.exception("webhook %s (%s) crashed", event_id, event_type)
            self.store.forget_webhook(event_id)
            return 500, {"detail": "webhook handling failed; retry"}
        if "ignored" in result:
            self.store.forget_webhook(event_id)  # nothing happened, so a later redelivery may succeed
        return status, result

    def _apply_webhook(self, event_type: str, resource: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        rid = str(resource.get("id") or "")
        related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
        if event_type == "CHECKOUT.ORDER.APPROVED":
            entry = self._find(order_id=rid)
        elif event_type == "PAYMENT.AUTHORIZATION.VOIDED":
            entry = self._find(authorization_id=rid)
        elif event_type in _CAPTURE_EVENTS:
            entry = self._find(authorization_id=related.get("authorization_id"), order_id=related.get("order_id"))
        else:
            return 200, {"ignored": event_type}
        if entry is None:
            return 200, {"ignored": "unknown resource"}
        trustee, contract = entry
        with self._deal_lock(contract.deal_id):
            if event_type == "CHECKOUT.ORDER.APPROVED":
                decision = trustee.on_approved(contract)
            elif event_type == "PAYMENT.AUTHORIZATION.VOIDED":
                decision = trustee.on_external_void(contract, "webhook " + event_type)
            elif event_type == "PAYMENT.CAPTURE.COMPLETED":
                decision = trustee.on_external_capture(contract, rid, "webhook " + event_type)
            else:
                decision = trustee.on_paypal_alert(
                    contract, event_type, f"PayPal reported {event_type} for capture {rid}"
                )
            self._persist(trustee, contract)
        return 200, {"handled": decision.action, "deal_id": contract.deal_id, "state": trustee.deal.state.value}

    # ------------------------------------------------------------------ sweep

    def sweep(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Run the deadline sweeper once, then poll PayPal for payer approvals; persist what acted."""
        with self._lock:
            unpersisted = list(self._unpersisted)
        for deal_id in unpersisted:
            entry = self._entry(deal_id)
            if entry is not None:
                self._persist(*entry)
        try:
            self.store.flush_backlog()
        except Exception:  # noqa: BLE001 - reported by `durability`; the sweep itself must go on
            log.exception("sweep: ledger backlog still not persisted")
        results = self.sweeper.tick(now)
        acted: list[tuple[str, str]] = []
        for deal_id, decision in results:
            entry = self._entry(deal_id)
            if entry is not None:
                self._persist(*entry)
            acted.append((deal_id, decision.action))
        acted.extend(self._poll_approvals(now))
        return acted

    def _poll_approvals(self, now: datetime | None) -> list[tuple[str, str]]:
        """Local dev gets no webhooks: ask PayPal whether awaiting orders were approved.
        At most MAX_APPROVAL_POLLS reads per sweep, rotating so a backlog cannot starve newer deals."""
        with self._lock:
            waiting = [(t, c) for t, c in self._deals.values() if t.awaiting_approval]
        if not waiting:
            return []
        start = self._poll_cursor % len(waiting)
        batch = (waiting[start:] + waiting[:start])[:MAX_APPROVAL_POLLS]
        self._poll_cursor += len(batch)
        acted: list[tuple[str, str]] = []
        for trustee, contract in batch:
            try:
                order_id = trustee.snapshot()["escrow"]["order_id"]
                if str(self.paypal.get_order(order_id).get("status")) != "APPROVED":
                    continue
                with self._deal_lock(contract.deal_id):
                    decision = trustee.on_approved(contract, now)
                    if decision.action == "error" and not is_transient_paypal(trustee.last_paypal_status):
                        # PayPal refused for good (e.g. an invalid payee). Retrying every sweep only
                        # burns attempts until PayPal locks the order; nothing is held, so close it.
                        log.warning(
                            "authorize refused for %s (%s %s); closing the deal",
                            contract.deal_id,
                            trustee.last_paypal_status,
                            trustee.last_paypal_issue,
                        )
                        decision = trustee.on_approval_cancelled(contract)
                    self._persist(trustee, contract)
                acted.append((contract.deal_id, decision.action))
            except Exception:  # noqa: BLE001 - one bad order must not stop the sweep
                log.exception("approval poll failed for %s", contract.deal_id)
        return acted


def _parse_when(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now()


def _authority_view(authority: dict[str, Any] | None) -> dict[str, Any]:
    """Who authorized this payment: a human at PayPal, or the principal's intent mandate. Public, no secrets."""
    if not authority:
        return {"human_present": True, "intent_mandate_digest": None, "outcome": "human_approval", "policy": []}
    return {
        "human_present": bool(authority.get("human_present", True)),
        "intent_mandate_digest": authority.get("intent_digest"),
        "outcome": authority.get("outcome"),
        "policy": authority.get("policy") or [],
    }


_CAPTURE_EVENTS = frozenset(
    {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.REFUNDED"}
)


def operator_token_from_env() -> str | None:
    return os.environ.get("TRUSTEE_OPERATOR_TOKEN") or None
