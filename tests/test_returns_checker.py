"""The returns-dataset checker: the buyer's criterion 2 and 3, run on delivered bytes.

The checker is the only thing standing between a junk export and the seller's
money, so every rule gets a test of its own. The fixtures here are generated
in place; the two real goods files are checked at the end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trustee.arena.runner import RETURNS_COLUMNS, check_returns_dataset, default_checkers
from trustee.arena.scenario import FIXTURES

GOOD_ID = "returns-dataset-2026q3"
HEADER = ",".join(RETURNS_COLUMNS)


def _row(i: int, **overrides: str) -> str:
    fields = {
        "order_id": f"NW-2026-{i:06d}",
        "sku": "TEE-CRW-BLK-M",
        "category": "tops",
        "order_date": "2026-08-10",
        "return_date": "2026-08-12",
        "reason_code": "DAMAGED",
        "refund_usd": "20.00",
    }
    fields.update(overrides)
    return ",".join(fields[c] for c in RETURNS_COLUMNS)


def _csv(rows: list[str], header: str = HEADER) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _valid_rows(n: int = 5000) -> list[str]:
    return [_row(i) for i in range(1, n + 1)]


def _write(tmp_path: Path, payload: bytes) -> Path:
    path = tmp_path / "returns-2026q3.csv"
    path.write_bytes(payload)
    return path


def _check(tmp_path: Path, payload: bytes) -> tuple[bool, str]:
    return check_returns_dataset(_write(tmp_path, payload), GOOD_ID)


# ---------------------------------------------------------------- happy path


def test_valid_dataset_passes(tmp_path: Path):
    assert _check(tmp_path, _csv(_valid_rows())) == (True, "schema ok, 5000 rows")


def test_real_honest_fixture_passes():
    ok, detail = check_returns_dataset(FIXTURES / "goods" / "returns-2026q3.csv", GOOD_ID)
    assert ok and detail == "schema ok, 5000 rows"


def test_real_junk_fixture_fails_on_schema():
    ok, detail = check_returns_dataset(FIXTURES / "goods" / "returns-2026q3-junk.csv", GOOD_ID)
    assert not ok
    assert detail.startswith("failed: schema mismatch")
    assert "refund_amount" in detail


def test_checker_is_registered_for_the_demo_good():
    assert default_checkers()[GOOD_ID] is check_returns_dataset


# ------------------------------------------------------------- schema failures


def test_renamed_column_fails_on_header(tmp_path: Path):
    renamed = HEADER.replace("refund_usd", "refund_amount")
    ok, detail = _check(tmp_path, _csv(_valid_rows(), header=renamed))
    assert not ok and detail.startswith("failed: schema mismatch")


def test_reordered_columns_fail_on_header(tmp_path: Path):
    reordered = ",".join(reversed(RETURNS_COLUMNS))
    ok, detail = _check(tmp_path, _csv(_valid_rows(), header=reordered))
    assert not ok and "schema mismatch" in detail


def test_wrong_field_count_fails(tmp_path: Path):
    rows = _valid_rows(5000)
    rows[99] += ",extra"
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and "line 101 has 8 fields" in detail


# ------------------------------------------------------------- row-level rules


def test_short_file_fails_on_row_count(tmp_path: Path):
    ok, detail = _check(tmp_path, _csv(_valid_rows(312)))
    assert not ok and detail == "failed: 312 rows, need at least 5000"


def test_exactly_the_minimum_row_count_passes(tmp_path: Path):
    assert _check(tmp_path, _csv(_valid_rows(5000)))[0] is True


def test_duplicate_order_id_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[700] = _row(1)  # line 702 reuses the id on line 2
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and detail.startswith("failed: duplicate order_id NW-2026-000001")


def test_blank_order_id_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[3] = _row(4, order_id="  ")
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and detail == "failed: blank order_id at line 5"


def test_non_numeric_refund_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[0] = _row(1, refund_usd="twenty")
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and "not a number at line 2" in detail


def test_blank_refund_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[0] = _row(1, refund_usd="")
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and detail == "failed: blank refund_usd at line 2"


def test_negative_refund_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[0] = _row(1, refund_usd="-1.00")
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and "non-negative" in detail


def test_return_before_order_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[0] = _row(1, order_date="2026-08-10", return_date="2026-08-09")
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and detail == "failed: return_date before order_date at line 2"


def test_bad_iso_date_fails(tmp_path: Path):
    rows = _valid_rows()
    rows[0] = _row(1, order_date="10/08/2026")
    ok, detail = _check(tmp_path, _csv(rows))
    assert not ok and detail == "failed: bad ISO date at line 2"


# ------------------------------------------------------------- unreadable input


def test_non_csv_bytes_fail(tmp_path: Path):
    ok, detail = _check(tmp_path, b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    assert not ok and detail.startswith("failed: not readable as UTF-8")


def test_plain_text_fails_on_header(tmp_path: Path):
    ok, detail = _check(tmp_path, b"this is not a dataset\n")
    assert not ok and detail.startswith("failed: schema mismatch")


def test_empty_file_fails(tmp_path: Path):
    ok, detail = _check(tmp_path, b"")
    assert not ok and detail.startswith("failed: schema mismatch")


def test_missing_file_fails(tmp_path: Path):
    ok, detail = check_returns_dataset(tmp_path / "absent.csv", GOOD_ID)
    assert not ok and detail == "failed: artifact missing: absent.csv"


@pytest.mark.parametrize("bad", [b"\xff\xfe\x00", b"order_id,\xc3"])
def test_invalid_utf8_fails(tmp_path: Path, bad: bytes):
    ok, detail = _check(tmp_path, bad)
    assert not ok and "failed" in detail
