"""Identity primitives: canonical bytes, Ed25519 sign/verify, registries, signed protocol objects."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trustee.identity import AgentKey, AgentRecord, AgentRegistry, PrincipalRegistry, canonical_json, verify
from trustee.protocol import Accept, Attest, Contract, GoodSpec, verify_signature

GOOD = GoodSpec(good_id="g", description="d", acceptance_criteria=["c"], deliverable_ref="goods/x.zip")


def make_contract(price: str = "10.00") -> Contract:
    accept = Accept(
        sender="buyer-1", responds_to="m", good=GOOD, price=Decimal(price), deadline=datetime(2030, 1, 1), deal_id="d1"
    )
    return Contract.from_accept(accept, seller="seller-1")


def test_canonical_json_is_deterministic():
    a = {"b": 1, "a": Decimal("1.50"), "t": datetime(2030, 1, 2, 3, 4, 5)}
    b = {"t": datetime(2030, 1, 2, 3, 4, 5), "a": Decimal("1.50"), "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == b'{"a":"1.50","b":1,"t":"2030-01-02T03:04:05"}'


def test_canonical_json_keeps_unicode_and_models():
    assert canonical_json({"k": "é"}) == '{"k":"é"}'.encode()
    assert canonical_json(GOOD) == canonical_json(GOOD.model_dump(mode="json"))


def test_sign_and_verify_round_trip():
    key = AgentKey.generate()
    sig = key.sign(b"hello")
    assert verify(key.public_key_b64, sig, b"hello")
    assert not verify(key.public_key_b64, sig, b"hello!")


def test_wrong_key_fails():
    sig = AgentKey.generate().sign(b"x")
    assert not verify(AgentKey.generate().public_key_b64, sig, b"x")


@pytest.mark.parametrize("pub,sig", [("", ""), ("not base64!!", "also not"), ("AAAA", "AAAA")])
def test_verify_never_raises_on_garbage(pub, sig):
    assert verify(pub, sig, b"x") is False


def test_private_key_round_trip():
    key = AgentKey.generate()
    again = AgentKey.from_private_bytes(key.private_bytes())
    assert again.public_key_b64 == key.public_key_b64


def test_registry_refuses_key_swap_but_allows_idempotent_register():
    reg = AgentRegistry()
    k1, k2 = AgentKey.generate(), AgentKey.generate()
    rec = AgentRecord("a", k1.public_key_b64, "p", frozenset({"buyer"}))
    reg.register(rec)
    reg.register(rec)
    with pytest.raises(ValueError):
        reg.register(AgentRecord("a", k2.public_key_b64, "p", frozenset({"buyer"})))
    assert reg.require("a").public_key == k1.public_key_b64
    with pytest.raises(KeyError):
        reg.require("nobody")
    assert reg.verify("nobody", k1.sign(b"x"), b"x") is False
    assert reg.verify("a", None, b"x") is False
    assert reg.verify("a", k1.sign(b"x"), b"x") is True


def test_principal_registry_refuses_key_swap():
    reg = PrincipalRegistry()
    k1, k2 = AgentKey.generate(), AgentKey.generate()
    reg.register("acme", k1.public_key_b64)
    with pytest.raises(ValueError):
        reg.register("acme", k2.public_key_b64)
    assert reg.verify("acme", k1.sign(b"m"), b"m")
    assert not reg.verify("acme", k2.sign(b"m"), b"m")
    assert not reg.verify("other", k1.sign(b"m"), b"m")


def test_message_signature_and_tamper():
    key = AgentKey.generate()
    reg = AgentRegistry()
    reg.register(AgentRecord("buyer-1", key.public_key_b64, "p", frozenset({"buyer"})))
    att = Attest(sender="buyer-1", deal_id="d1", verdict="accept", reason="ok").sign(key)
    assert att.signature and verify_signature(att, reg)
    assert Attest(sender="buyer-1", deal_id="d1", verdict="accept", reason="ok").signature is None
    tampered = att.model_copy(update={"verdict": "reject"})
    assert not verify_signature(tampered, reg)
    # signing a copy leaves the original unsigned
    unsigned = Attest(sender="buyer-1", deal_id="d1", verdict="accept", reason="ok")
    unsigned.sign(key)
    assert unsigned.signature is None


def test_message_signed_by_wrong_key_or_claimed_sender_fails():
    real, thief = AgentKey.generate(), AgentKey.generate()
    reg = AgentRegistry()
    reg.register(AgentRecord("buyer-1", real.public_key_b64, "p", frozenset({"buyer"})))
    forged = Attest(sender="buyer-1", deal_id="d1", verdict="accept", reason="ok").sign(thief)
    assert not verify_signature(forged, reg)


def test_contract_digest_tracks_terms_not_signatures():
    base = make_contract()
    key = AgentKey.generate()
    assert base.digest() == base.sign("buyer-1", key).digest()
    assert base.digest() == base.model_copy(update={"transcript_ref": "elsewhere"}).digest()
    assert base.digest() != make_contract("10.01").digest()
    assert base.digest() != base.model_copy(update={"deadline": base.deadline + timedelta(seconds=1)}).digest()


def test_contract_party_signatures_verify_only_for_the_signed_terms():
    bk, sk = AgentKey.generate(), AgentKey.generate()
    reg = AgentRegistry()
    reg.register(AgentRecord("buyer-1", bk.public_key_b64, "p", frozenset({"buyer"})))
    reg.register(AgentRecord("seller-1", sk.public_key_b64, "p", frozenset({"seller"})))
    c = make_contract().sign("buyer-1", bk).sign("seller-1", sk)
    assert c.verify_party("buyer-1", reg) and c.verify_party("seller-1", reg)
    repriced = c.model_copy(update={"price": Decimal("1.00")})
    assert not repriced.verify_party("buyer-1", reg)
    assert not make_contract().verify_party("buyer-1", reg)
