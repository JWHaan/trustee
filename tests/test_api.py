"""Arena service tests: the run-to-completion path, SSE framing, and replay.

The SSE bridge is the highest-risk code in the arena (worker thread -> event
loop), so it gets tested as a stream, not as a mocked callback: a real run, a
real HTTP request, and an assertion on the bytes that come back.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from trustee.arena.api import create_app
from trustee.ledger import Ledger
from trustee.paypal import PayPalClient


def make_client() -> httpx.AsyncClient:
    app = create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arena")


async def wait_for(client: httpx.AsyncClient, run_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        snap = (await client.get(f"/api/runs/{run_id}")).json()
        if snap["state"] not in ("RUNNING",):
            return snap
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


async def collect_sse(client: httpx.AsyncClient, run_id: str, timeout: float = 15.0) -> tuple[list[dict], dict]:
    events: list[dict] = []
    done: dict = {}
    async with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if name == "done":
                    done = payload
                    return events, done
                events.append(payload)
    raise AssertionError("stream closed without a done event")


# --------------------------------------------------------------------- basics


def test_healthz_and_scenarios():
    async def go() -> None:
        async with make_client() as client:
            health = (await client.get("/healthz")).json()
            assert health["ok"] is True
            assert health["paypal"] == "dry-run"

            scenarios = (await client.get("/api/scenarios")).json()
            names = [s["name"] for s in scenarios["scenarios"]]
            assert names == ["honest", "cheat", "forged", "outside", "a2a"]
            assert scenarios["modes"] == ["auto", "live", "scripted"]

    asyncio.run(go())


def test_unknown_scenario_is_404():
    async def go() -> None:
        async with make_client() as client:
            resp = await client.post("/api/runs", json={"scenario": "rugpull"})
            assert resp.status_code == 404

    asyncio.run(go())


def test_unknown_run_is_404():
    async def go() -> None:
        async with make_client() as client:
            assert (await client.get("/api/runs/run_nope")).status_code == 404
            assert (await client.get("/api/runs/run_nope/events")).status_code == 404

    asyncio.run(go())


# ------------------------------------------------------------------ full runs


def test_forged_run_over_the_api_reports_rejected_identity():
    async def go() -> None:
        async with make_client() as client:
            created = (await client.post("/api/runs", json={"scenario": "forged", "mode": "scripted"})).json()
            snap = await wait_for(client, created["run_id"])
            assert snap["outcome"] == "rejected_identity", snap
            assert snap["error"] is None
            rows = (await client.get("/api/ledger")).json()["rows"]
            assert [r["check"] for r in rows if r["kind"] == "identity"] == ["verified", "rejected"]
            assert not any(r["kind"] == "paypal" for r in rows)

    asyncio.run(go())


def test_honest_run_over_the_api_settles():
    async def go() -> None:
        async with make_client() as client:
            created = (await client.post("/api/runs", json={"scenario": "honest", "mode": "scripted"})).json()
            snap = await wait_for(client, created["run_id"])
            assert snap["state"] == "SETTLED", snap
            assert snap["price"] == "156.00"
            assert snap["attest"]["verdict"] == "accept"
            assert snap["events"] > 10

            ledger = (await client.get("/api/ledger")).json()
            assert ledger["count"] == snap["events"]
            assert any(r["kind"] == "paypal" for r in ledger["rows"])

    asyncio.run(go())


def test_cheat_run_over_the_api_voids():
    async def go() -> None:
        async with make_client() as client:
            created = (await client.post("/api/runs", json={"scenario": "cheat", "mode": "scripted"})).json()
            snap = await wait_for(client, created["run_id"])
            assert snap["state"] == "VOIDED", snap
            assert snap["ruling"]["decision"] == "release"

    asyncio.run(go())


# ------------------------------------------------------------------------ SSE


def test_sse_streams_rows_then_done():
    async def go() -> None:
        async with make_client() as client:
            created = (await client.post("/api/runs", json={"scenario": "honest", "mode": "scripted"})).json()
            rows, done = await collect_sse(client, created["run_id"])

            assert len(rows) == done["events"]
            assert done["state"] == "SETTLED"
            assert [r["seq"] for r in rows] == list(range(len(rows)))  # ordered, no gaps
            kinds = {r["kind"] for r in rows}
            assert {"state", "mandate", "paypal", "delivery", "msg"} <= kinds

    asyncio.run(go())


def test_sse_replays_a_finished_run():
    """Refresh after the run: same rows, same order, no second code path."""

    async def go() -> None:
        async with make_client() as client:
            created = (await client.post("/api/runs", json={"scenario": "cheat", "mode": "scripted"})).json()
            await wait_for(client, created["run_id"])

            first, done_a = await collect_sse(client, created["run_id"])
            second, done_b = await collect_sse(client, created["run_id"])

            assert [r["id"] for r in first] == [r["id"] for r in second]
            assert done_a["state"] == done_b["state"] == "VOIDED"

    asyncio.run(go())


def test_two_runs_do_not_bleed_into_each_other():
    async def go() -> None:
        async with make_client() as client:
            a = (await client.post("/api/runs", json={"scenario": "honest", "mode": "scripted"})).json()
            b = (await client.post("/api/runs", json={"scenario": "cheat", "mode": "scripted"})).json()
            snap_a = await wait_for(client, a["run_id"])
            snap_b = await wait_for(client, b["run_id"])

            assert snap_a["deal_id"] != snap_b["deal_id"]
            assert snap_a["state"] == "SETTLED"
            assert snap_b["state"] == "VOIDED"

            rows_a, _ = await collect_sse(client, a["run_id"])
            assert {r["deal"] for r in rows_a} == {snap_a["deal_id"]}

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["scripted"])
def test_mode_is_echoed_back(mode: str):
    async def go() -> None:
        async with make_client() as client:
            created = (await client.post("/api/runs", json={"scenario": "honest", "mode": mode})).json()
            assert created["mode"] == mode
            snap = await wait_for(client, created["run_id"])
            assert snap["effective_mode"] == mode

    asyncio.run(go())
