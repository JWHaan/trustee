"""Trustee checks the delivered file itself.

Without this, the buyer runs the checks and reports the result, and Trustee rules on the buyer's word: a buyer could
receive a correct file, report a failure and get refunded while keeping it. Here the seller hands the file to Trustee,
and Trustee runs the contract's checks on those exact bytes before anyone rules on anything:

  1. the file's sha256 equals the hash in the contract;
  2. the checker registered for the good passes (schema and row rules, the Channel3 market check, ...);
  3. for contracts with a pro-rata term, the counter says how many units are valid.

The result is a `Verification`. Trustee records it on the ledger, prefers it over the buyer's evidence when it rules,
and the buyer gets the file from Trustee only once the payment has settled (`DealService.download_deliverable`).

Files are stored by their sha256 (`BlobStore`), so a stored file is identified by its content. A seller's signed
`Delivery` names the hash; Trustee only ever checks the bytes under that hash. A seller who uploaded something for the
deal but signs a different hash has made a false claim, and the verification says so.

No checker imports or runs anything from the file: they read it as data.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .criteria import check_csv, checkable_csv
from .protocol import Contract, Delivery

log = logging.getLogger(__name__)

MAX_DELIVERABLE_BYTES = 25 * 1024 * 1024

Checker = Callable[..., tuple[bool, str]]  # (path, good_id[, contract=]) -> (ok, detail)
UnitCounter = Callable[..., tuple[int, int]]  # (path, good_id[, contract=]) -> (units_valid, units_total)


class BlobError(ValueError):
    """The upload was refused (empty or too large)."""


class BlobStore:
    """Delivered files, stored under their sha256, plus which hashes were uploaded for which deal."""

    def __init__(self, root: Path | str | None = None, max_bytes: int = MAX_DELIVERABLE_BYTES):
        if root is None:
            root = os.environ.get("TRUSTEE_BLOB_DIR") or tempfile.mkdtemp(prefix="trustee-blobs-")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._by_deal: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes, deal_id: str | None = None) -> str:
        if not data:
            raise BlobError("the file is empty")
        if len(data) > self.max_bytes:
            raise BlobError(f"the file is larger than {self.max_bytes} bytes")
        digest = hashlib.sha256(data).hexdigest()
        path = self.path(digest)
        if not path.exists():
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        if deal_id:
            with self._lock:
                self._by_deal.setdefault(deal_id, set()).add(digest)
        return digest

    def path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BlobError("not a sha256 hex digest")
        return self.root / digest

    def get(self, digest: str) -> bytes | None:
        try:
            path = self.path(digest.lower())
        except BlobError:
            return None
        return path.read_bytes() if path.is_file() else None

    def uploaded_for(self, deal_id: str) -> set[str]:
        with self._lock:
            return set(self._by_deal.get(deal_id, set()))


@dataclass
class Verification:
    """What Trustee found when it checked the delivered file. `passed` is the whole answer."""

    passed: bool
    hash_ok: bool
    sha256_expected: str | None
    sha256_received: str | None
    checks: list[dict[str, Any]] = field(default_factory=list)  # [{name, ok, detail}]
    units_valid: int | None = None
    units_total: int | None = None
    failed_clause: str | None = None  # the contract criterion that failed first, for the ruling
    by: str = "trustee"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Verification:
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})  # type: ignore[arg-type]

    def note(self) -> str:
        if self.passed:
            extra = f" and {len(self.checks)} contract check(s)" if self.checks else ""
            return f"Trustee checked the delivered file itself: the hash{extra} passed"
        failed = [c["detail"] for c in self.checks if not c["ok"]]
        if not self.hash_ok:
            failed.insert(0, "the file's sha256 does not match the contract")
        return "Trustee checked the delivered file itself: " + "; ".join(failed)

    def evidence(self) -> dict[str, str]:
        """The same keys a buyer's attestation uses, so the partial-settlement math reads Trustee's counts."""
        out = {"verified_by": "trustee", "sha256_check": "match" if self.hash_ok else "mismatch"}
        if self.units_valid is not None and self.units_total is not None:
            out["units_valid"] = str(self.units_valid)
            out["units_total"] = str(self.units_total)
        return out


def _clause(contract: Contract, needle: str) -> str | None:
    return next((c for c in contract.good.acceptance_criteria if needle in c.lower()), None)


def _call(fn: Callable[..., Any], path: Path, contract: Contract) -> Any:
    if getattr(fn, "needs_contract", False):
        return fn(path, contract.good.good_id, contract=contract)
    return fn(path, contract.good.good_id)


class DeliveryVerifier:
    """Runs the contract's checks on the bytes Trustee holds for a delivery."""

    def __init__(
        self,
        blobs: BlobStore,
        checkers: Mapping[str, Checker] | None = None,
        counters: Mapping[str, UnitCounter] | None = None,
    ):
        self.blobs = blobs
        self.checkers = dict(checkers if checkers is not None else default_checkers())
        self.counters = dict(counters if counters is not None else default_counters())

    def verify(self, contract: Contract, delivery: Delivery) -> Verification | None:
        """None when the seller gave Trustee no file for this deal: the buyer's check is then the only one."""
        signed = delivery.content_hash.lower()
        expected = (contract.good.sha256 or "").lower() or None
        if self.blobs.get(signed) is None:
            uploaded = self.blobs.uploaded_for(contract.deal_id)
            if not uploaded:
                return None
            # The seller uploaded a file for this deal but signed a different hash: the signed claim is false.
            received = sorted(uploaded)[0]
            return Verification(
                passed=False,
                hash_ok=received == expected,
                sha256_expected=expected,
                sha256_received=received,
                checks=[
                    {
                        "name": "signed hash",
                        "ok": False,
                        "detail": "the seller signed a hash for a file it did not hand over",
                    }
                ],
                failed_clause=_clause(contract, "sha256") or "sha256 matches contract",
            )
        path = self.blobs.path(signed)
        hash_ok = expected is not None and signed == expected
        checks: list[dict[str, Any]] = []
        checker = self.checkers.get(contract.good.good_id)
        if checker is None and checkable_csv(contract):
            # No checker made for this good: run the contract's own schema and row criteria on the file.
            checker = check_csv
        if checker is not None:
            try:
                ok, detail = _call(checker, path, contract)
            except Exception as exc:  # noqa: BLE001 - a checker that crashes is a failed check, not a crash
                log.warning("checker for %s failed", contract.good.good_id, exc_info=True)
                ok, detail = False, f"failed: the check could not run ({type(exc).__name__})"
            checks.append({"name": contract.good.good_id, "ok": bool(ok), "detail": str(detail)})
        units_valid = units_total = None
        counter = self.counters.get(contract.good.good_id)
        if contract.partial is not None and counter is not None:
            try:
                units_valid, units_total = _call(counter, path, contract)
            except Exception:  # noqa: BLE001 - no counts: the ordinary refund path applies
                log.warning("unit counter for %s failed", contract.good.good_id, exc_info=True)
        passed = hash_ok and all(c["ok"] for c in checks)
        failed_clause = None
        if not hash_ok:
            failed_clause = _clause(contract, "sha256") or "sha256 matches contract"
        elif not passed:
            detail = next(c["detail"] for c in checks if not c["ok"]).lower()
            needle = "market" if "market" in detail else "schema" if "schema" in detail else "row"
            failed_clause = _clause(contract, needle) or _clause(contract, "row") or "acceptance criteria"
        return Verification(
            passed=passed,
            hash_ok=hash_ok,
            sha256_expected=expected,
            sha256_received=signed,
            checks=checks,
            units_valid=units_valid,
            units_total=units_total,
            failed_clause=failed_clause,
        )


def default_checkers() -> dict[str, Checker]:
    """The checkers the demo goods use (CSV schema and rows; the Channel3 price-feed market check)."""
    from .arena.runner import default_checkers as arena_checkers

    return dict(arena_checkers())


def default_counters() -> dict[str, UnitCounter]:
    from .arena.runner import default_counters as arena_counters

    return dict(arena_counters())
