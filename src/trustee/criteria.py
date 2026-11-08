"""Acceptance criteria: drafted from a person's request by AI, kept only when code can tie them to the words, and
executed by Trustee on the delivered file.

A buyer says what they want ("Q3 returns data, at least 5,000 orders, with order_id, sku and refund_usd columns, no
blank refunds"). `draft_criteria` turns that into contract criteria in a fixed form that a machine can check:

    sha256 matches contract                      the file is the one the seller published (when a hash is known)
    schema: order_id, sku, refund_usd            the CSV header lists these columns, in this order
    at least 5000 rows, no blank refund_usd      row count, and fields that must be filled in every row
    market check: ...                            prices compared with live market offers (price feeds)

The model may propose; the code decides. A column is kept only if its name appears in the request, a row count only if
that number appears in it. Anything else the model suggests is kept as a plain-language criterion marked "a person
decides", because Trustee cannot check it. Without a model, the same rules read the request directly.

`check_csv` executes these criteria on a CSV, so Trustee verifies a delivery against the exact words both sides signed,
for any data good, not only the demo datasets with their own checkers.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .protocol import Contract

log = logging.getLogger(__name__)

Kind = Literal["hash", "schema", "rows", "market", "manual"]
MARKET_CRITERION = "market check: 20 sampled rows priced within max(5%, $1) of live Channel3 offers"
_IDENT = re.compile(r"\b[a-z][a-z0-9_]*\b")
_ROWS = re.compile(r"(\d[\d,]*)\s*(?:rows|orders|records|items|lines|entries)", re.I)
_COLUMNS = re.compile(r"(?:columns?|fields?)\s*[:(]?\s*([a-z0-9_,\s]+?)(?:[).;]|$|\bno\b|\band\s+no\b)", re.I)
_LIST_SEP = re.compile(r"\s*,\s*|\s+(?:or|and)\s+")  # whole words only: "order_id" must not split on "or"
_MARKET = re.compile(r"\b(market price|live price|current price|competitor price|price feed)", re.I)


class CriteriaError(ValueError):
    """The request is empty or yields nothing a contract could use."""


@dataclass
class Criterion:
    text: str  # what goes in the contract, word for word
    kind: Kind
    checkable: bool  # True: Trustee checks it on the file; False: a person decides
    why: str  # where it came from, for the person reviewing the draft


@dataclass
class CriteriaDraft:
    request: str
    criteria: list[Criterion]
    source: Literal["llm", "rules"]
    notes: list[str] = field(default_factory=list)

    def texts(self) -> list[str]:
        return [c.text for c in self.criteria]

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "criteria": [asdict(c) for c in self.criteria],
            "source": self.source,
            "notes": list(self.notes),
        }


def _words(text: str) -> set[str]:
    return set(_IDENT.findall(text.lower()))


def _numbers(text: str) -> set[int]:
    return {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)}


def draft_criteria(request: str, llm: Any | None = None, *, sha256: str | None = None) -> CriteriaDraft:
    """Propose contract criteria from a person's request. Show the draft to both sides; they sign or edit it."""
    text = " ".join(request.split())
    if not text:
        raise CriteriaError("the request is empty")
    notes: list[str] = []
    proposal: dict[str, Any] = {}
    if llm is not None:
        try:
            proposal = llm.complete_json(
                "You turn a buyer's request for a data file into JSON for an acceptance checker. Reply with ONLY an "
                "object: columns (list of column names, in order), min_rows (int or null), required (list of column "
                "names that must never be blank), market_check (bool: prices must match live market offers), "
                "other (list of short sentences for anything else the buyer requires). Use only what the request says.",
                [{"role": "user", "content": text}],
            )
            if not isinstance(proposal, dict):
                proposal = {}
        except Exception as exc:  # noqa: BLE001 - a failed model never blocks drafting
            log.info("criteria drafting model unavailable: %s", exc)
            proposal = {}
    source: Literal["llm", "rules"] = "llm" if proposal else "rules"
    said = _words(text)
    numbers = _numbers(text)

    def stated_names(values: Any) -> list[str]:
        kept = []
        for v in values if isinstance(values, list) else []:
            name = str(v).strip().lower()
            if name in said and name not in kept:
                kept.append(name)
            elif name:
                notes.append(f"ignored column {name!r}: not in the request")
        return kept

    columns = stated_names(proposal.get("columns"))
    if not columns:
        m = _COLUMNS.search(text)
        if m:
            columns = [c for c in re.split(r"[,\s]+(?:and\s+)?", m.group(1).strip().lower()) if c and c != "and"]
    min_rows: int | None = None
    raw_rows = proposal.get("min_rows")
    if isinstance(raw_rows, int | str) and str(raw_rows).replace(",", "").isdigit():
        n = int(str(raw_rows).replace(",", ""))
        if n in numbers:
            min_rows = n
        else:
            notes.append(f"ignored row count {n}: not in the request")
    if min_rows is None:
        m = _ROWS.search(text)
        min_rows = int(m.group(1).replace(",", "")) if m else None
    required = [c for c in stated_names(proposal.get("required")) if c in columns]
    if not required and columns:
        blank = re.search(r"no blank\s+([a-z0-9_]+(?:(?:\s*,\s*|\s+(?:or|and)\s+)[a-z0-9_]+)*)", text, re.I)
        if blank:
            required = [c for c in re.split(_LIST_SEP, blank.group(1).lower()) if c in columns]

    out: list[Criterion] = []
    if sha256:
        out.append(Criterion("sha256 matches contract", "hash", True, "the seller published this file's hash"))
    if columns:
        out.append(Criterion("schema: " + ", ".join(columns), "schema", True, "columns named in the request"))
    if min_rows is not None or required:
        rows = f"at least {min_rows} rows" if min_rows is not None else "every row"
        blanks = f", no blank {' or '.join(required)}" if required else ""
        out.append(Criterion(f"{rows}{blanks}", "rows", True, "row count and required fields in the request"))
    if proposal.get("market_check") is True or _MARKET.search(text):
        out.append(Criterion(MARKET_CRITERION, "market", True, "the request asks for market prices"))
    for other in proposal.get("other") or []:
        if isinstance(other, str) and other.strip():
            out.append(Criterion(other.strip()[:200], "manual", False, "suggested by the model; a person decides"))
    if not out:
        raise CriteriaError("nothing checkable found: name the columns, a row count, or a file hash")
    return CriteriaDraft(request=text, criteria=out, source=source, notes=notes)


# ------------------------------------------------------------------------------------- executing the criteria

_SCHEMA = re.compile(r"^schema:\s*(.+)$", re.I)
_ROW_RULE = re.compile(r"^(?:at least\s+(\d[\d,]*)\s+rows|every row)(?:,\s*no blank\s+(.+))?$", re.I)


def checkable_csv(contract: Contract) -> bool:
    """True when the contract's criteria include a schema or row rule that `check_csv` can execute."""
    return any(_SCHEMA.match(c.strip()) or _ROW_RULE.match(c.strip()) for c in contract.good.acceptance_criteria)


def check_csv(path: Path, good_id: str, *, contract: Contract) -> tuple[bool, str]:
    """Run the contract's schema and row criteria on a delivered CSV. Reads it as text; nothing in it is executed."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"failed: not a readable UTF-8 file ({type(exc).__name__})"
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return False, "failed: the file is empty"
    header = [h.strip().lower() for h in rows[0]]
    body = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    for raw in contract.good.acceptance_criteria:
        crit = raw.strip()
        schema = _SCHEMA.match(crit)
        if schema:
            want = [c.strip().lower() for c in schema.group(1).split(",") if c.strip()]
            if header[: len(want)] != want:
                return False, f"failed: schema mismatch, header is {','.join(header)[:120]}"
            continue
        rule = _ROW_RULE.match(crit)
        if rule:
            if rule.group(1) is not None and len(body) < int(rule.group(1).replace(",", "")):
                return False, f"failed: {len(body)} rows, the contract asks for at least {rule.group(1)}"
            for name in re.split(_LIST_SEP, (rule.group(2) or "").lower()):
                if not name:
                    continue
                if name not in header:
                    return False, f"failed: required column {name} is missing"
                i = header.index(name)
                for n, r in enumerate(body, start=2):
                    if i >= len(r) or not r[i].strip():
                        return False, f"failed: blank {name} at line {n}"
    return True, f"{len(body)} rows; schema and row criteria met"


check_csv.needs_contract = True  # type: ignore[attr-defined]
