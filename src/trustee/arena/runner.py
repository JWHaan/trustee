"""DealRunner — one scenario, one deal, one tape.

This is pure composition: it constructs the agents, sequences them, and lets
the shared `Ledger` tell the story. No policy lives here. Where it *does* add
value is in the four things the plan assumed would be free:

1. **Deal attribution.** `Negotiator._coerce` mints a fresh `deal_id` whenever
   the LLM omits one, so offer, counter, and contract would each carry a
   different id and the grid would split one deal into four. The runner pins
   the id onto every message (`_pin`).
2. **Correct parties.** `Contract.from_accept` assumes the *buyer* is the
   accepting sender. When the seller accepts — the normal outcome of the floor
   clamp — buyer and seller both come out as the seller, and the mandate gate
   then refuses a perfectly good deal. The runner names both parties.
3. **State transitions on the tape.** `Deal.transition` records into
   `deal.history` but never into the ledger, so the grid's signature
   `state_from -> state_to` column had no data source. `_flush_states` drains
   the history after each phase.
4. **Mode.** `scripted` never touches the network, `live` fails loudly,
   `auto` falls back to scripted and says so on the tape.

5. **Signed identity.** Each run mints fresh Ed25519 keys for buyer, seller,
   arbiter and the mandates' principal, registers them, signs the demo
   mandates, has both parties sign the contract, and signs the seller's
   Delivery and the buyer's Attest before the Trustee sees them. The agents
   (buyer.py/seller.py) know nothing about keys: the runner signs on their
   behalf, standing in for each agent's own signer. The Trustee's verifications
   land on the tape as `identity` rows.

6. **Attacks and waits.** A scenario's `attack` ("forged_signature") makes the runner sign
   the seller's side of the contract with an unregistered impostor key: the Trustee must
   refuse before PayPal is touched. When PayPal needs the payer to approve the order, the run
   publishes the approve link and polls until the buyer approves, cancels, or the wait times
   out (`approval.py`). The `outside` flow hands the whole deal to agents that drive the
   public /v1 API over HTTP (`outside.py`).

A fallback mints a new deal id rather than replaying over the failed one: a
half-written tape from a dead live attempt and a complete scripted tape must
not share a key, or the grid groups two different stories together.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from ..agents.arbiter import ArbiterAgent
from ..agents.buyer import ArtifactStore, BuyerAgent
from ..agents.seller import SellerAgent
from ..agents.trustee import Trustee, TrusteeDecision, is_transient_paypal
from ..identity import AgentKey, AgentRecord, AgentRegistry, PrincipalRegistry
from ..ledger import Event, Ledger
from ..llm import LLM
from ..mandate import Mandate, MandateEngine
from ..network import Northwind, Tidewater
from ..paypal import EscrowOrder, PayPalClient, PayPalError, sandbox_emails
from ..protocol import Accept, Contract, Counter, Offer
from ..state import Deal, DealState
from .a2a_flow import A2AFlow
from .approval import ApprovalGate, ApprovalWait, wait_for_approval
from .fixtures import load_mandates
from .outside import OutsideFlow
from .scenario import FIXTURES, Scenario
from .scripted import ScriptedLLM

log = logging.getLogger(__name__)

BUYER_ID = "buyer-agent-1"
SELLER_ID = "seller-agent-1"
ARBITER_ID = "arbiter-agent-1"

MODES = ("auto", "live", "scripted")
ATTACKS = ("forged_signature",)
MAX_ROUNDS = 3  # buyer+seller exchanges after the opening offer

Handler = Callable[[Event], None]
Checker = Callable[[Path, str], "tuple[bool, str]"]
UnitCounter = Callable[[Path, str], "tuple[int, int]"]


# --------------------------------------------------------------------- results


@dataclass
class RunResult:
    run_id: str
    deal_id: str
    scenario: str
    mode: str  # what the caller asked for
    effective_mode: str  # what actually ran: live | scripted
    state: str
    price: str | None = None
    turns: int = 0
    contract: dict[str, Any] | None = None
    escrow: dict[str, Any] | None = None
    ruling: dict[str, Any] | None = None
    attest: dict[str, Any] | None = None
    error: str | None = None
    fallback_from: str | None = None
    outcome: str | None = None  # why a run ended short of a held escrow, e.g. "rejected_identity"
    settlement: dict[str, Any] | None = None  # pro-rata settlement: captured, released, units

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {
            "run_id": self.run_id,
            "deal_id": self.deal_id,
            "scenario": self.scenario,
            "mode": self.mode,
            "effective_mode": self.effective_mode,
            "state": self.state,
            "price": self.price,
            "turns": self.turns,
            "contract": self.contract,
            "escrow": self.escrow,
            "ruling": self.ruling,
            "attest": self.attest,
            "error": self.error,
            "fallback_from": self.fallback_from,
            "outcome": self.outcome,
        }
        if self.settlement is not None:
            snap["settlement"] = self.settlement
        return snap


# ---------------------------------------------------------------------- runner


class DealRunner:
    def __init__(
        self,
        ledger: Ledger,
        paypal: PayPalClient,
        mandates: MandateEngine | None = None,
        store: ArtifactStore | None = None,
        checkers: dict[str, Checker] | None = None,
        *,
        self_client: Callable[[], httpx.Client] | None = None,
        close_self_client: bool = False,
        platform_ledger: Ledger | None = None,
        operator_token: str | None = None,
        approvals: ApprovalGate | None = None,
        approval_wait: ApprovalWait | None = None,
        network: tuple[Northwind, Tidewater] | None = None,
    ):
        self.ledger = ledger
        self.paypal = paypal
        # No engine supplied -> the runner owns the demo mandates and signs them
        # per run. A caller-supplied engine is used exactly as given.
        self._owns_mandates = mandates is None
        self.mandates = mandates or MandateEngine(load_mandates())
        self.store = store or ArtifactStore.load(FIXTURES)
        self.checkers = checkers if checkers is not None else default_checkers()
        self.counters = default_counters()
        # Only the `outside` flow uses these: an HTTP client for the arena's own /v1 API and the
        # platform's ledger, whose rows for the deal are copied onto the run's tape.
        self.self_client = self_client
        self._close_self_client = close_self_client
        self.platform_ledger = platform_ledger
        self.operator_token = operator_token
        self.approvals = approvals
        self.approval_wait = approval_wait if approval_wait is not None else ApprovalWait.from_env()
        # Only the `a2a` flow uses this: the mounted Northwind and Tidewater agents (see a2a_flow.py).
        self.network = network

    # ---------------------------------------------------------------- public

    def run(
        self,
        scenario: Scenario,
        mode: str = "auto",
        on_event: Handler | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        """Execute one deal. Never raises for a run failure: that comes back in
        `result.error`, so the caller always has something to render."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        requested = mode
        effective, note = self._resolve_mode(mode)
        try:
            return self._run_once(scenario, requested, effective, on_event, run_id, note=note)
        except Exception as exc:  # noqa: BLE001 - the API must always get a result
            if effective == "live" and requested in ("auto", "live"):
                log.warning("live run failed (%s); retrying scripted", exc)
                return self._run_once(
                    scenario,
                    requested,
                    "scripted",
                    on_event,
                    run_id,
                    note=f"live failed, falling back: {str(exc)[:160]}",
                    fallback_from="live",
                )
            return RunResult(
                run_id=run_id or f"run_{_short()}",
                deal_id="",
                scenario=scenario.name,
                mode=requested,
                effective_mode=effective,
                state="ERROR",
                error=str(exc),
            )

    # --------------------------------------------------------------- internals

    def _resolve_mode(self, mode: str) -> tuple[str, str]:
        if mode == "scripted":
            return "scripted", "replay only, no model calls"
        if not os.environ.get("LLM_API_KEY"):
            # No key configured: don't burn a 401 round-trip to find out.
            return "scripted", "no model key — replaying a recorded negotiation"
        return "live", "live model negotiation"

    def _run_once(
        self,
        scenario: Scenario,
        requested: str,
        effective: str,
        on_event: Handler | None,
        run_id: str | None,
        *,
        note: str,
        fallback_from: str | None = None,
    ) -> RunResult:
        deal_id = f"deal_{scenario.name}_{_short()}"
        run_id = run_id or f"run_{_short()}"
        deal = Deal(deal_id, on_transition=_state_recorder(self.ledger, deal_id))
        unsubscribe = self.ledger.subscribe(_only(deal_id, on_event)) if on_event else (lambda: None)
        # Per-run inbox: deliveries must never overwrite the fixture inventory,
        # or the second run would attest a file the first run corrupted.
        with tempfile.TemporaryDirectory(prefix="trustee-inbox-") as tmp:
            store = ArtifactStore(goods_dir=self.store.goods_dir, manifest=self.store.manifest, inbox=Path(tmp))
            try:
                self.ledger.append(
                    deal_id,
                    "arena",
                    "run",
                    event="run_started",
                    scenario=scenario.name,
                    mode=requested,
                    effective_mode=effective,
                    note=note,
                )
                if scenario.flow == "outside":
                    return self._drive_outside(scenario, deal_id, run_id, requested, effective, fallback_from, store)
                if scenario.flow == "a2a":
                    return self._drive_a2a(scenario, deal_id, run_id, requested, effective, fallback_from, store)
                return self._drive(scenario, deal, run_id, requested, effective, fallback_from, store)
            finally:
                unsubscribe()

    def _drive(
        self,
        scenario: Scenario,
        deal: Deal,
        run_id: str,
        requested: str,
        effective: str,
        fallback_from: str | None,
        store: ArtifactStore,
    ) -> RunResult:
        deal_id = deal.deal_id
        if scenario.attack is not None and scenario.attack not in ATTACKS:
            raise ValueError(f"unknown attack {scenario.attack!r} (known: {', '.join(ATTACKS)})")
        llm = self._build_llm(scenario, effective)

        deal.transition(DealState.NEGOTIATING, "negotiation opened")

        buyer = BuyerAgent(
            agent_id=BUYER_ID,
            llm=llm,
            good=scenario.good,
            budget=scenario.budget,
            store=store,
            checkers=self.checkers,
            ledger=self.ledger,
            counters=self.counters,
        )
        seller = SellerAgent(
            agent_id=SELLER_ID,
            llm=llm,
            good=scenario.good,
            floor_price=scenario.floor,
            target_price=scenario.ask,
            store=store,
            ledger=self.ledger,
        )

        ident = self._mint_identity()
        mandates = self._run_mandates(ident)

        contract, turns = self._negotiate(scenario, buyer, seller, deal_id)
        if contract is None:  # pragma: no cover - the cap always yields a contract
            return RunResult(
                run_id, deal_id, scenario.name, requested, effective, deal.state.value,
                error="no contract within the move cap", fallback_from=fallback_from,
            )  # fmt: skip

        # The impostor claims to be the seller but holds a key nobody registered.
        seller_key = AgentKey.generate() if scenario.attack == "forged_signature" else ident.keys[SELLER_ID]
        contract = contract.sign(BUYER_ID, ident.keys[BUYER_ID]).sign(SELLER_ID, seller_key)

        buyer_email, seller_email = sandbox_emails()
        trustee = Trustee(
            deal=deal,
            mandates=mandates,
            paypal=self.paypal,
            llm=llm,
            ledger=self.ledger,
            buyer_email=buyer_email,
            seller_email=seller_email,
            judge=ArbiterAgent(ARBITER_ID, llm=llm, ledger=self.ledger),
            registry=ident.registry,
        )

        decision = trustee.on_contract(contract)
        stopped: str | None = None
        if decision.action == "awaiting_approval":
            decision, stopped = self._await_payer(deal_id, trustee, contract, decision)
        if decision.action != "escrowed":
            # Identity or mandate refusal, PayPal failure, or a payer who never approved: all
            # honest, all terminal here.
            outcome = stopped or decision.action
            self.ledger.append(deal_id, "arena", "run", event="run_stopped", note=outcome)
            result = self._result(
                run_id, deal, scenario, requested, effective, contract, decision.escrow, fallback_from
            )
            result.turns = turns
            result.outcome = outcome
            clean = (
                "rejected_mandate",
                "rejected_identity",
                "approval_timeout",
                "approval_cancelled",
                "approval_failed",
            )
            result.error = None if outcome in clean else decision.action
            return result

        delivery = seller.deliver(contract, cheat=scenario.cheat).sign(ident.keys[SELLER_ID])
        trustee.on_delivery(delivery, contract)

        attest = buyer.verify_delivery(contract, delivery).sign(ident.keys[BUYER_ID])
        trustee.on_attest(attest, contract)

        result = self._result(run_id, deal, scenario, requested, effective, contract, decision.escrow, fallback_from)
        result.attest = {"verdict": attest.verdict, "reason": attest.reason, "evidence": attest.evidence}
        result.turns = turns
        self.ledger.append(
            deal_id,
            "arena",
            "run",
            event="run_finished",
            state=deal.state.value,
            price=result.price,
            note=f"{effective} run, {turns} negotiation turns",
        )
        return result

    # ------------------------------------------------------------- approval

    def _await_payer(
        self, deal_id: str, trustee: Trustee, contract: Contract, decision: TrusteeDecision
    ) -> tuple[TrusteeDecision, str | None]:
        """Hold the run open while the buyer approves the order at PayPal.

        Returns the decision that ends the wait and, when the run must stop, why. Timeout and
        cancel both go through `on_approval_cancelled`: nothing was held, so no PayPal call."""
        assert decision.escrow is not None
        order_id = decision.escrow.order_id
        wait = self.approval_wait
        self.ledger.append(
            deal_id,
            "arena",
            "run",
            event="awaiting_approval",
            order_id=order_id,
            approve_url=decision.escrow.approve_url or "",
            wait_s=int(wait.timeout_seconds),
            note="Approve this payment in the PayPal sandbox (test money). Funds are held only after approval.",
        )
        cancel = self.approvals.track(order_id) if self.approvals is not None else threading.Event()
        latest = decision
        refused: str | None = None  # PayPal's reason, once it has said no for good

        def attempt() -> bool:
            nonlocal latest, refused
            try:
                status = str(self.paypal.get_order(order_id).get("status"))
            except PayPalError as exc:
                log.warning("approval poll failed for %s: %s", order_id, exc)
                return False  # transient: ask again on the next tick
            if status != "APPROVED":
                return False
            latest = trustee.on_approved(contract)
            if latest.action != "error":
                return True
            if is_transient_paypal(trustee.last_paypal_status):
                return False  # a network or 5xx failure leaves state unchanged; the next tick retries
            # PayPal refused for good (e.g. an invalid payee locks the order after a few attempts): stop asking.
            refused = trustee.last_paypal_issue or f"HTTP {trustee.last_paypal_status}"
            return True

        try:
            result = wait_for_approval(attempt, cancel, wait)
        finally:
            if self.approvals is not None:
                self.approvals.release(order_id)
        if result == "approved" and refused is not None:
            self.ledger.append(
                deal_id,
                "arena",
                "run",
                event="approval_failed",
                note=f"PayPal refused to authorize the approved order: {refused}. Nothing was held.",
            )
            return trustee.on_approval_cancelled(contract), "approval_failed"
        if result == "approved":
            if latest.action == "escrowed":
                self.ledger.append(deal_id, "arena", "run", event="payer_approved", note="buyer approved in PayPal")
            return latest, None
        note = "buyer cancelled at PayPal" if result == "cancelled" else "no approval before the timeout"
        self.ledger.append(deal_id, "arena", "run", event=f"approval_{result}", note=note)
        return trustee.on_approval_cancelled(contract), f"approval_{result}"

    # ----------------------------------------------------------- outside agents

    def _drive_outside(
        self,
        scenario: Scenario,
        deal_id: str,
        run_id: str,
        requested: str,
        effective: str,
        fallback_from: str | None,
        store: ArtifactStore,
    ) -> RunResult:
        if self.self_client is None:
            raise RuntimeError("the outside scenario needs a self_client (the arena's own /v1 API)")
        http = self.self_client()
        unforward = self._forward_platform(deal_id)
        try:
            flow = OutsideFlow(
                scenario=scenario,
                deal_id=deal_id,
                suffix=deal_id.rsplit("_", 1)[-1],
                ledger=self.ledger,
                paypal=self.paypal,
                http=http,
                operator_token=self.operator_token or os.environ.get("TRUSTEE_OPERATOR_TOKEN") or None,
                store=store,
                checkers=self.checkers,
                llm=self._build_llm(scenario, effective),
                wait=self.approval_wait,
            )
            done = flow.run()
        finally:
            unforward()
            if self._close_self_client:
                http.close()
        return self._http_flow_result(
            scenario, deal_id, run_id, requested, effective, fallback_from, done, "agents on /v1 over HTTP"
        )

    def _drive_a2a(
        self,
        scenario: Scenario,
        deal_id: str,
        run_id: str,
        requested: str,
        effective: str,
        fallback_from: str | None,
        store: ArtifactStore,
    ) -> RunResult:
        if self.self_client is None or self.network is None:
            raise RuntimeError("the a2a scenario needs a self_client and the mounted network agents")
        http = self.self_client()
        unforward = self._forward_platform(deal_id)
        try:
            northwind, tidewater = self.network
            flow = A2AFlow(
                scenario=scenario,
                deal_id=deal_id,
                ledger=self.ledger,
                paypal=self.paypal,
                http=http,
                operator_token=self.operator_token or os.environ.get("TRUSTEE_OPERATOR_TOKEN") or None,
                store=store,
                checkers=self.checkers,
                llm=self._build_llm(scenario, effective),
                wait=self.approval_wait,
                northwind=northwind,
                tidewater=tidewater,
            )
            done = flow.run()
        finally:
            unforward()
            if self._close_self_client:
                http.close()
        return self._http_flow_result(
            scenario, deal_id, run_id, requested, effective, fallback_from, done, "A2A agents over HTTP"
        )

    def _http_flow_result(
        self,
        scenario: Scenario,
        deal_id: str,
        run_id: str,
        requested: str,
        effective: str,
        fallback_from: str | None,
        done: Any,
        label: str,
    ) -> RunResult:
        view = done.view
        result = RunResult(
            run_id=run_id,
            deal_id=deal_id,
            scenario=scenario.name,
            mode=requested,
            effective_mode=effective,
            state=view["state"],
            price=_agreed_price(scenario, view),
            turns=done.turns,
            contract=view["contract"],
            escrow=view["escrow"],
            ruling=view["ruling"],
            attest=done.attest,
            fallback_from=fallback_from,
            outcome=done.outcome,
        )
        if done.outcome is not None:
            self.ledger.append(deal_id, "arena", "run", event="run_stopped", note=done.outcome)
        else:
            self.ledger.append(
                deal_id,
                "arena",
                "run",
                event="run_finished",
                state=result.state,
                price=result.price,
                note=f"{effective} run, {label}",
            )
        return result

    def _forward_platform(self, deal_id: str) -> Callable[[], None]:
        """Copy the platform ledger's rows for this deal onto the run's tape, in order."""
        source = self.platform_ledger
        if source is None or source is self.ledger:
            return lambda: None

        def forward(ev: Event) -> None:
            if ev.deal_id == deal_id:
                self.ledger.append(ev.deal_id, ev.actor, ev.kind, **ev.payload)

        return source.subscribe(forward)

    # ------------------------------------------------------------ negotiation

    def _negotiate(
        self,
        scenario: Scenario,
        buyer: BuyerAgent,
        seller: SellerAgent,
        deal_id: str,
    ) -> tuple[Contract | None, int]:
        opening = scenario.opening_price
        offer = _pin(buyer.open_deal(opening, SELLER_ID), deal_id)
        self.ledger.append(
            deal_id,
            BUYER_ID,
            "msg",
            event="offer",
            price=str(offer.price),
            note=f"opens {offer.price} — {_pct(offer.price, scenario.ask)} under the {scenario.ask} ask",
        )
        turns = 1
        their: Offer | Counter = offer

        for _ in range(MAX_ROUNDS):
            reply = _pin(seller.respond_to(their), deal_id)
            turns += 1
            if isinstance(reply, Accept):
                return self._contract_from(reply, scenario, deal_id), turns
            their = reply

            answer = _pin(buyer.respond(their.model_dump(mode="json"), opening, max_turns=2), deal_id)
            turns += 1
            if isinstance(answer, Accept):
                self.ledger.append(
                    deal_id, BUYER_ID, "msg", event="accept", price=str(answer.price), note="accepts the standing price"
                )
                return self._contract_from(answer, scenario, deal_id), turns
            self.ledger.append(
                deal_id,
                BUYER_ID,
                "msg",
                event="counter",
                price=str(answer.price),
                note=f"counters {answer.price} against {their.price}",
            )
            their = answer

        # Move cap: end the chat at the standing price. The mandate + escrow
        # gate still vets the result, so the cap can end a conversation but
        # never force a bad deal through.
        self.ledger.append(
            deal_id,
            "arena",
            "run",
            event="cap_reached",
            price=str(their.price),
            note="move cap — accepting the standing price",
        )
        accept = Accept(
            sender=BUYER_ID,
            responds_to=getattr(their, "msg_id", "cap"),
            good=scenario.good,
            price=their.price,
            deadline=their.deadline,
            deal_id=deal_id,
        )
        return self._contract_from(accept, scenario, deal_id), turns

    @staticmethod
    def _contract_from(accept: Accept, scenario: Scenario, deal_id: str) -> Contract:
        contract = Contract.from_accept(accept, seller=SELLER_ID)
        # `from_accept` calls the accepting sender the buyer; that is only true
        # when the buyer accepts. Name both parties explicitly. The good is the
        # scenario's own spec, including its contract sha256, so an LLM-minted
        # good can never reach the contract.
        update: dict[str, Any] = {"deal_id": deal_id, "buyer": BUYER_ID, "seller": SELLER_ID, "good": scenario.good}
        if scenario.partial is not None:
            update["partial"] = scenario.partial  # the pro-rata term is part of the scenario, signed with the contract
        return contract.model_copy(update=update)

    # ---------------------------------------------------------------- helpers

    def _mint_identity(self) -> _RunIdentity:
        """Fresh keys per run: nothing signed here is valid in any other run."""
        keys = {agent_id: AgentKey.generate() for agent_id in (BUYER_ID, SELLER_ID, ARBITER_ID)}
        registry = AgentRegistry()
        for agent_id, role, email in (
            (BUYER_ID, "buyer", sandbox_emails()[0]),
            (SELLER_ID, "seller", sandbox_emails()[1]),
            (ARBITER_ID, "arbiter", None),
        ):
            registry.register(
                AgentRecord(
                    agent_id=agent_id,
                    public_key=keys[agent_id].public_key_b64,
                    principal="demo-principal",
                    roles=frozenset({role}),
                    paypal_email=email,
                )
            )
        return _RunIdentity(keys=keys, registry=registry, principal_key=AgentKey.generate())

    def _run_mandates(self, ident: _RunIdentity) -> MandateEngine:
        """The caller's engine as given, or the demo mandates signed by this run's principal."""
        if not self._owns_mandates:
            return self.mandates
        principals = PrincipalRegistry()
        signed: dict[str, Mandate] = {}
        for agent_id in (BUYER_ID, SELLER_ID):
            m = self.mandates.get(agent_id)
            if m is None:
                continue
            principals.register(m.principal, ident.principal_key.public_key_b64)
            signed[agent_id] = m.sign(ident.principal_key)
        return MandateEngine(signed, principals=principals)

    def _build_llm(self, scenario: Scenario, effective: str) -> LLM:
        if effective == "scripted":
            return ScriptedLLM.from_file(FIXTURES / "transcripts" / f"{scenario.transcript}.json")
        return LLM()

    def _result(
        self,
        run_id: str,
        deal: Deal,
        scenario: Scenario,
        requested: str,
        effective: str,
        contract: Contract,
        escrow: EscrowOrder | None,
        fallback_from: str | None,
    ) -> RunResult:
        ruling = next(
            (
                {
                    "decision": e.payload.get("decision"),
                    "clause": e.payload.get("clause"),
                    "reasoning": e.payload.get("reasoning"),
                    "objective": e.payload.get("objective"),
                }
                for e in reversed(self.ledger.tail(deal.deal_id, kinds=("ruling",)))
            ),
            None,
        )
        settled = self.ledger.tail(deal.deal_id, kinds=("settlement",))
        return RunResult(
            run_id=run_id,
            deal_id=deal.deal_id,
            scenario=scenario.name,
            mode=requested,
            effective_mode=effective,
            state=deal.state.value,
            settlement=(
                {k: v for k, v in settled[-1].payload.items() if k not in ("note", "price")} if settled else None
            ),
            price=str(contract.price),
            contract=contract.key_clauses(),
            escrow=(
                {
                    "order_id": escrow.order_id,
                    "authorization_id": escrow.authorization_id,
                    "status": escrow.status,
                    "approve_url": escrow.approve_url,
                }
                if escrow
                else None
            ),
            ruling=ruling,
            fallback_from=fallback_from,
        )


# ------------------------------------------------------------------ module bits


@dataclass
class _RunIdentity:
    keys: dict[str, AgentKey]
    registry: AgentRegistry
    principal_key: AgentKey


def _pin(msg: Any, deal_id: str) -> Any:
    """Force the run's deal id onto a protocol message the LLM re-minted."""
    if getattr(msg, "deal_id", None) == deal_id:
        return msg
    return msg.model_copy(update={"deal_id": deal_id})


def _state_recorder(ledger: Ledger, deal_id: str) -> Callable[[str, str, str], None]:
    def record(reason: str, frm: str, to: str) -> None:
        ledger.append(deal_id, "trustee", "state", event="transition", state_from=frm, state_to=to, note=reason)

    return record


def _only(deal_id: str, handler: Handler | None) -> Handler:
    def emit(ev: Event) -> None:
        if handler is not None and ev.deal_id == deal_id:
            handler(ev)

    return emit


def _agreed_price(scenario: Scenario, view: dict[str, Any]) -> str:
    """Outside agents agreed their price off-platform (it is in the scenario); A2A agents negotiated it, so it is
    read back from the contract Trustee holds."""
    if scenario.flow == "a2a":
        quoted = str((view.get("contract") or {}).get("price") or "")
        if quoted:
            return quoted.split(" ")[0]
    return str(scenario.agreed_price or scenario.ask)


def _short() -> str:
    return uuid.uuid4().hex[:8]


def _pct(price: Decimal, base: Decimal) -> str:
    if not base:
        return "0%"
    return f"{(1 - price / base) * 100:.0f}%"


RETURNS_COLUMNS = ("order_id", "sku", "category", "order_date", "return_date", "reason_code", "refund_usd")
RETURNS_MIN_ROWS = 5000


def default_checkers() -> dict[str, Checker]:
    return {"returns-dataset-2026q3": check_returns_dataset}


def default_counters() -> dict[str, UnitCounter]:
    return {"returns-dataset-2026q3": count_returns_units}


def check_returns_dataset(artifact: Path, good_id: str) -> tuple[bool, str]:
    """Criteria 2 and 3, executed against the delivered bytes.

    Reads the CSV with the stdlib reader and checks it against the contract's
    schema and row rules. Nothing in the file is imported or run, so a seller's
    content never executes on the buyer's machine. Every failure starts with
    "failed", the marker the arbiter keys its objective-breach rule on.
    """
    import csv
    import io
    from datetime import date

    if not artifact.exists():
        return False, f"failed: artifact missing: {artifact.name}"
    try:
        text = artifact.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"failed: not readable as UTF-8 text ({type(exc).__name__})"

    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None or tuple(header) != RETURNS_COLUMNS:
        found = ",".join(header or [])[:120]
        return False, f"failed: schema mismatch, header is {found or '(empty)'}"

    seen: set[str] = set()
    rows = 0
    for line_no, row in enumerate(reader, start=2):
        if len(row) != len(RETURNS_COLUMNS):
            return False, f"failed: line {line_no} has {len(row)} fields, expected {len(RETURNS_COLUMNS)}"
        rec = dict(zip(RETURNS_COLUMNS, row, strict=True))
        order_id = rec["order_id"].strip()
        if not order_id:
            return False, f"failed: blank order_id at line {line_no}"
        if order_id in seen:
            return False, f"failed: duplicate order_id {order_id} at line {line_no}"
        seen.add(order_id)
        refund_raw = rec["refund_usd"].strip()
        if not refund_raw:
            return False, f"failed: blank refund_usd at line {line_no}"
        try:
            refund = Decimal(refund_raw)
        except InvalidOperation:
            return False, f"failed: refund_usd '{refund_raw[:20]}' is not a number at line {line_no}"
        if not refund.is_finite() or refund < 0:
            return False, f"failed: refund_usd must be a non-negative amount at line {line_no}"
        try:
            ordered = date.fromisoformat(rec["order_date"])
            returned = date.fromisoformat(rec["return_date"])
        except ValueError:
            return False, f"failed: bad ISO date at line {line_no}"
        if returned < ordered:
            return False, f"failed: return_date before order_date at line {line_no}"
        rows += 1

    if rows < RETURNS_MIN_ROWS:
        return False, f"failed: {rows} rows, need at least {RETURNS_MIN_ROWS}"
    return True, f"schema ok, {rows} rows"


def count_returns_units(artifact: Path, good_id: str) -> tuple[int, int]:
    """(valid rows, total rows) of a delivered returns CSV, by the same per-row rules as the checker.

    A row is valid when it has the right field count, a non-blank order_id not seen on an earlier row,
    a non-negative numeric refund_usd and ISO dates with return_date on or after order_date. A missing
    or unreadable file, or a wrong header, counts zero valid rows.
    """
    import csv
    import io
    from datetime import date

    try:
        text = artifact.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return 0, 0
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None or tuple(header) != RETURNS_COLUMNS:
        return 0, sum(1 for _ in reader)
    seen: set[str] = set()
    valid = total = 0
    for row in reader:
        total += 1
        if len(row) != len(RETURNS_COLUMNS):
            continue
        rec = dict(zip(RETURNS_COLUMNS, row, strict=True))
        order_id = rec["order_id"].strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        try:
            refund = Decimal(rec["refund_usd"].strip())
            ordered = date.fromisoformat(rec["order_date"])
            returned = date.fromisoformat(rec["return_date"])
        except (InvalidOperation, ValueError):
            continue
        if not refund.is_finite() or refund < 0 or returned < ordered:
            continue
        valid += 1
    return valid, total
