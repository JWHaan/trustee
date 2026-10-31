"""Agent and principal identity — who actually said this.

In an open A2A network an agent id is just a string: anyone can claim to be
`buyer-agent-1`, and a mandate is self-asserted. This module makes identity a
key, not a name:

  * every agent holds an Ed25519 key; its public half is registered under its id;
  * every principal (the human/company behind a mandate) holds one too;
  * messages, contracts and mandates are signed over `canonical_json` bytes, so
    two honest parties always hash the same document to the same bytes.

Verification never raises on bad input. A malformed key, signature or payload is
just "not verified" — the caller decides what to do, and a garbage signature can
never crash the money path.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, frozenset | set):
        return sorted(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"cannot canonicalize {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    """Deterministic bytes for `obj`: sorted keys, no whitespace, utf-8.

    Decimal -> str, datetime -> isoformat, pydantic models -> model_dump(mode="json").
    """
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default).encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


class AgentKey:
    """An Ed25519 private key. The only object that can produce a signature."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._private = private_key

    @classmethod
    def generate(cls) -> AgentKey:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> AgentKey:
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    def private_bytes(self) -> bytes:
        return self._private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )

    @property
    def public_key_b64(self) -> str:
        raw = self._private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return _b64e(raw)

    def sign(self, data: bytes) -> str:
        return _b64e(self._private.sign(data))


def verify(public_key_b64: str, signature_b64: str, data: bytes) -> bool:
    """True only for a valid signature by that key over `data`. Never raises."""
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64d(public_key_b64))
        key.verify(_b64d(signature_b64), data)
    except (InvalidSignature, ValueError, TypeError, binascii.Error, UnicodeError):
        return False
    return True


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    public_key: str  # urlsafe b64 of the raw 32-byte Ed25519 public key
    principal: str
    roles: frozenset[str] = frozenset()  # "buyer" | "seller" | "arbiter" | ...
    paypal_email: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentRegistry:
    """agent_id -> public key. A key swap is refused: first registration wins."""

    def __init__(self) -> None:
        self._records: dict[str, AgentRecord] = {}
        self._lock = threading.Lock()

    def register(self, record: AgentRecord) -> None:
        with self._lock:
            existing = self._records.get(record.agent_id)
            if existing is not None and existing.public_key != record.public_key:
                raise ValueError(f"agent {record.agent_id!r} is already registered with a different key")
            self._records[record.agent_id] = record

    def get(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._records.get(agent_id)

    def require(self, agent_id: str) -> AgentRecord:
        record = self.get(agent_id)
        if record is None:
            raise KeyError(f"unknown agent {agent_id!r}")
        return record

    def verify(self, agent_id: str, signature: str | None, data: bytes) -> bool:
        record = self.get(agent_id)
        if record is None or not signature:
            return False
        return verify(record.public_key, signature, data)


class PrincipalRegistry:
    """principal -> public key, for checking who signed a mandate."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, principal: str, public_key: str) -> None:
        with self._lock:
            existing = self._keys.get(principal)
            if existing is not None and existing != public_key:
                raise ValueError(f"principal {principal!r} is already registered with a different key")
            self._keys[principal] = public_key

    def get(self, principal: str) -> str | None:
        with self._lock:
            return self._keys.get(principal)

    def verify(self, principal: str, signature: str | None, data: bytes) -> bool:
        key = self.get(principal)
        if key is None or not signature:
            return False
        return verify(key, signature, data)
