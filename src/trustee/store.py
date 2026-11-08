"""Durable SQLite store for Trustee deals, agents, mandates, events and webhooks.

Why this exists: today every deal, agent and mandate lives in process memory. If the service
restarts while PayPal holds a buyer's money, Trustee forgets the deal exists and nothing ever
captures or voids it. This store is where those records survive a restart, so open deals can
be resumed.

This module only builds the store. Wiring it into the API and the Trustee agent happens in a
later wave. Until then, events reach the store only when a caller explicitly calls `attach`.

Stdlib `sqlite3` only, one connection guarded by a lock. Every write is one transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trustee.ledger import Event, Ledger

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
TERMINAL_STATES: tuple[str, ...] = ("SETTLED", "VOIDED")

_V1_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS agents (
        agent_id TEXT PRIMARY KEY,
        public_key TEXT NOT NULL,
        principal TEXT NOT NULL,
        roles TEXT NOT NULL,
        paypal_email TEXT,
        metadata TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS principals (
        principal TEXT PRIMARY KEY,
        public_key TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS mandates (
        agent_id TEXT PRIMARY KEY,
        principal TEXT NOT NULL,
        body TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS deals (
        deal_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        contract TEXT,
        snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS deals_state ON deals(state)",
    """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        deal_id TEXT,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS events_deal ON events(deal_id)",
    """CREATE TABLE IF NOT EXISTS webhook_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        resource_id TEXT,
        received_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
)


# Additive tables, created on every open with IF NOT EXISTS. They do not bump SCHEMA_VERSION: a database
# written by this build stays readable by one that predates them (it never looks at these tables).
_ADDITIVE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS push_configs (
        config_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        url TEXT NOT NULL,
        token TEXT,
        auth_scheme TEXT,
        auth_credentials TEXT,
        dialect TEXT NOT NULL DEFAULT 'v1',
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS push_configs_task ON push_configs(task_id)",
    # Autonomy (human-not-present payments). `funding_sources.vault_id` is a PayPal secret: it is stored here and
    # read only by `Store.get_vault_id`, never by a listing, an API response, the ledger or a receipt.
    """CREATE TABLE IF NOT EXISTS funding_sources (
        funding_ref TEXT PRIMARY KEY,
        principal TEXT NOT NULL,
        setup_token_id TEXT,
        vault_id TEXT,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS funding_sources_setup ON funding_sources(setup_token_id)",
    """CREATE TABLE IF NOT EXISTS intents (
        agent_id TEXT PRIMARY KEY,
        principal TEXT NOT NULL,
        body TEXT NOT NULL,
        digest TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS receipts (
        deal_id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        issued_at TEXT NOT NULL
    )""",
)

# Columns added to existing tables after v1 shipped. Each is applied once, if the column is missing.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("events", "prev_hash", "TEXT"),
    ("events", "hash", "TEXT"),
)


class StoreConflict(Exception):
    """A write would silently replace an identity already on record."""


class StoreError(Exception):
    """The database cannot be used, for example because a newer build wrote it."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class Store:
    """Durable record of deals, agents, mandates, events and webhook deliveries."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        # Ledger events that could not be written yet, oldest first. They are retried, in order, before any newer
        # event; `flush_backlog` raises while any remain, which is how the money path fails closed.
        self._backlog: deque[Any] = deque()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. Serialised by the lock; rolled back on any exception."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            if current > SCHEMA_VERSION:
                raise StoreError(f"{self.path} has schema v{current}; this build understands v{SCHEMA_VERSION}")
            if current < 1:
                for statement in _V1_STATEMENTS:
                    conn.execute(statement)
            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            for statement in _ADDITIVE_STATEMENTS:
                conn.execute(statement)
            for table, column, decl in _ADDITIVE_COLUMNS:
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- agents --------------------------------------------------------------

    def put_agent(
        self,
        agent_id: str,
        public_key: str,
        principal: str,
        roles: Iterable[str],
        paypal_email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register an agent. Re-registering with a different key raises StoreConflict."""
        roles_json = _dump(list(roles))
        metadata_json = _dump(metadata or {})
        with self._tx() as conn:
            row = conn.execute("SELECT public_key FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO agents (agent_id, public_key, principal, roles, paypal_email, metadata, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, public_key, principal, roles_json, paypal_email, metadata_json, _now()),
                )
            elif row["public_key"] != public_key:
                raise StoreConflict(f"agent {agent_id!r} is registered with a different public key")
            else:
                conn.execute(
                    "UPDATE agents SET principal = ?, roles = ?, paypal_email = ?, metadata = ? WHERE agent_id = ?",
                    (principal, roles_json, paypal_email, metadata_json, agent_id),
                )

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return _agent_dict(row) if row else None

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM agents ORDER BY created_at, agent_id").fetchall()
        return [_agent_dict(r) for r in rows]

    # -- principals ----------------------------------------------------------

    def put_principal(self, principal: str, public_key: str) -> None:
        """Register a principal. Re-registering with a different key raises StoreConflict."""
        with self._tx() as conn:
            row = conn.execute("SELECT public_key FROM principals WHERE principal = ?", (principal,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO principals (principal, public_key, created_at) VALUES (?, ?, ?)",
                    (principal, public_key, _now()),
                )
            elif row["public_key"] != public_key:
                raise StoreConflict(f"principal {principal!r} is registered with a different public key")

    def get_principal(self, principal: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM principals WHERE principal = ?", (principal,)).fetchone()
        return dict(row) if row else None

    # -- mandates ------------------------------------------------------------

    def put_mandate(self, agent_id: str, principal: str, body: dict[str, Any]) -> None:
        """Store a mandate. Writing again clears a previous revocation."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO mandates (agent_id, principal, body, revoked, updated_at) VALUES (?, ?, ?, 0, ?)"
                " ON CONFLICT(agent_id) DO UPDATE SET principal = excluded.principal, body = excluded.body,"
                " revoked = 0, updated_at = excluded.updated_at",
                (agent_id, principal, _dump(body), _now()),
            )

    def get_mandate(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM mandates WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return {
            "agent_id": row["agent_id"],
            "principal": row["principal"],
            "body": _load(row["body"]),
            "revoked": bool(row["revoked"]),
            "updated_at": row["updated_at"],
        }

    def revoke_mandate(self, agent_id: str) -> bool:
        """Mark a mandate revoked. Returns False when no mandate exists for the agent."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE mandates SET revoked = 1, updated_at = ? WHERE agent_id = ?",
                (_now(), agent_id),
            )
            return cur.rowcount == 1

    # -- deals ---------------------------------------------------------------

    def upsert_deal(
        self,
        deal_id: str,
        state: str,
        contract: dict[str, Any] | None,
        snapshot: dict[str, Any],
    ) -> None:
        """Insert or update a deal. A None contract keeps the stored contract; the snapshot is replaced."""
        now = _now()
        contract_json = None if contract is None else _dump(contract)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO deals (deal_id, state, contract, snapshot, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(deal_id) DO UPDATE SET state = excluded.state,"
                " contract = COALESCE(excluded.contract, deals.contract),"
                " snapshot = excluded.snapshot, updated_at = excluded.updated_at",
                (deal_id, state, contract_json, _dump(snapshot), now, now),
            )

    def get_deal(self, deal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
        return _deal_dict(row) if row else None

    def list_deals(self, states: Iterable[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Deals newest first, optionally filtered to the given states."""
        sql = "SELECT * FROM deals"
        params: list[Any] = []
        if states is not None:
            wanted = list(states)
            if not wanted:
                return []
            sql += f" WHERE state IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_deal_dict(r) for r in rows]

    def open_deals(self) -> list[dict[str, Any]]:
        """Every deal that is not SETTLED or VOIDED, oldest first. This is what a restart resumes."""
        placeholders = ",".join("?" * len(TERMINAL_STATES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM deals WHERE state NOT IN ({placeholders}) ORDER BY created_at ASC, rowid ASC",
                TERMINAL_STATES,
            ).fetchall()
        return [_deal_dict(r) for r in rows]

    # -- events --------------------------------------------------------------

    def append_event(
        self,
        ts: str,
        deal_id: str | None,
        actor: str,
        kind: str,
        payload: dict[str, Any],
        prev_hash: str = "",
        hash: str = "",  # noqa: A002 - the column name
    ) -> int:
        """Persist one event and return its sequence number."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, deal_id, actor, kind, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, deal_id, actor, kind, _dump(payload), prev_hash or None, hash or None),
            )
            seq = cur.lastrowid
        if seq is None:
            raise StoreError("events insert returned no sequence number")
        return seq

    def events(self, deal_id: str | None = None, after_seq: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """Events in ascending sequence order, after `after_seq`, optionally for one deal."""
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[Any] = [after_seq]
        if deal_id is not None:
            sql += " AND deal_id = ?"
            params.append(deal_id)
        sql += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "seq": r["seq"],
                "ts": r["ts"],
                "deal_id": r["deal_id"],
                "actor": r["actor"],
                "kind": r["kind"],
                "payload": _load(r["payload"]),
                "prev_hash": r["prev_hash"] or "",
                "hash": r["hash"] or "",
            }
            for r in rows
        ]

    def attach(self, ledger: Ledger) -> Callable[[], None]:
        """Persist every event the ledger emits. Returns the unsubscribe callable.

        A storage failure does not raise into the ledger: the event joins an ordered backlog and is retried before the
        next one, so nothing is dropped and each deal's hash chain lands in order. Before money moves, the platform
        calls `flush_backlog`, which raises while the backlog cannot be written: no PayPal call is made on top of
        events that are not durable.
        """

        def _persist(ev: Event) -> None:
            with self._lock:
                self._backlog.append(ev)
                try:
                    self._drain()
                except Exception:  # noqa: BLE001 - storage must never break the ledger
                    log.exception("store: failed to persist ledger event; %d waiting, will retry", len(self._backlog))

        return ledger.subscribe(_persist)

    def _drain(self) -> None:
        while self._backlog:
            ev = self._backlog[0]
            self.append_event(ev.ts, ev.deal_id, ev.actor, ev.kind, ev.payload, ev.prev_hash, ev.hash)
            self._backlog.popleft()

    def flush_backlog(self) -> None:
        """Write every waiting ledger event, oldest first. Raises StoreError while any cannot be written."""
        with self._lock:
            try:
                self._drain()
            except Exception as exc:
                raise StoreError(f"{len(self._backlog)} ledger event(s) not persisted: {exc}") from exc

    @property
    def backlog_size(self) -> int:
        with self._lock:
            return len(self._backlog)

    # -- push notification configs (A2A) -----------------------------------------

    def put_push_config(
        self,
        config_id: str,
        task_id: str,
        url: str,
        token: str | None = None,
        auth_scheme: str | None = None,
        auth_credentials: str | None = None,
        dialect: str = "v1",
    ) -> None:
        """Create or replace a push config. A config id already used by a different task raises StoreConflict."""
        with self._tx() as conn:
            row = conn.execute("SELECT task_id FROM push_configs WHERE config_id = ?", (config_id,)).fetchone()
            if row is not None and row["task_id"] != task_id:
                raise StoreConflict(f"push config {config_id!r} belongs to a different task")
            conn.execute(
                "INSERT INTO push_configs (config_id, task_id, url, token, auth_scheme, auth_credentials, dialect,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(config_id) DO UPDATE SET url = excluded.url, token = excluded.token,"
                " auth_scheme = excluded.auth_scheme, auth_credentials = excluded.auth_credentials,"
                " dialect = excluded.dialect",
                (config_id, task_id, url, token, auth_scheme, auth_credentials, dialect, _now()),
            )

    def get_push_config(self, task_id: str, config_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM push_configs WHERE task_id = ? AND config_id = ?", (task_id, config_id)
            ).fetchone()
        return dict(row) if row else None

    def list_push_configs(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM push_configs WHERE task_id = ? ORDER BY created_at, config_id", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_push_config(self, task_id: str, config_id: str) -> bool:
        """Remove a config. Returns False when there was none (deleting twice is harmless)."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM push_configs WHERE task_id = ? AND config_id = ?", (task_id, config_id))
            return cur.rowcount == 1

    def push_config_task_ids(self) -> set[str]:
        """Ids of every task that has at least one push config."""
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT task_id FROM push_configs").fetchall()
        return {r["task_id"] for r in rows}

    # -- key/value metadata (the Trustee signing key lives here when no env key is set) --------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def put_meta_if_absent(self, key: str, value: str) -> str:
        """Store `value` unless `key` is set; returns the value now stored (so two racing writers agree)."""
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, value))
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"])

    # -- funding sources (vaulted payment methods) ---------------------------------------------------------

    def put_funding(self, funding_ref: str, principal: str, setup_token_id: str | None, vault_id: str | None) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO funding_sources (funding_ref, principal, setup_token_id, vault_id, revoked, created_at)"
                " VALUES (?, ?, ?, ?, 0, ?)"
                " ON CONFLICT(funding_ref) DO UPDATE SET setup_token_id = excluded.setup_token_id,"
                " vault_id = excluded.vault_id",
                (funding_ref, principal, setup_token_id, vault_id, _now()),
            )

    def funding_public(self, funding_ref: str) -> dict[str, Any] | None:
        """A funding source WITHOUT its vault id: who owns it, whether it is usable, whether it was revoked."""
        with self._lock:
            row = self._conn.execute(
                "SELECT funding_ref, principal, setup_token_id, vault_id IS NOT NULL AS active_token, revoked,"
                " created_at FROM funding_sources WHERE funding_ref = ?",
                (funding_ref,),
            ).fetchone()
        if row is None:
            return None
        return {
            "funding_ref": row["funding_ref"],
            "principal": row["principal"],
            "setup_token_id": row["setup_token_id"],
            "vaulted": bool(row["active_token"]),
            "revoked": bool(row["revoked"]),
            "created_at": row["created_at"],
        }

    def funding_by_setup_token(self, setup_token_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT funding_ref FROM funding_sources WHERE setup_token_id = ?", (setup_token_id,)
            ).fetchone()
        return None if row is None else str(row["funding_ref"])

    def get_vault_id(self, funding_ref: str, principal: str) -> str | None:
        """The one reader of a vault id: only for an unrevoked source owned by `principal`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT vault_id FROM funding_sources WHERE funding_ref = ? AND principal = ? AND revoked = 0",
                (funding_ref, principal),
            ).fetchone()
        return None if row is None or row["vault_id"] is None else str(row["vault_id"])

    def revoke_funding(self, funding_ref: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("UPDATE funding_sources SET revoked = 1 WHERE funding_ref = ?", (funding_ref,))
            return cur.rowcount == 1

    # -- intent mandates -----------------------------------------------------------------------------------

    def put_intent(self, agent_id: str, principal: str, body: dict[str, Any], digest: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO intents (agent_id, principal, body, digest, revoked, updated_at) VALUES (?, ?, ?, ?, 0, ?)"
                " ON CONFLICT(agent_id) DO UPDATE SET principal = excluded.principal, body = excluded.body,"
                " digest = excluded.digest, revoked = 0, updated_at = excluded.updated_at",
                (agent_id, principal, _dump(body), digest, _now()),
            )

    def get_intent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM intents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return {
            "agent_id": row["agent_id"],
            "principal": row["principal"],
            "body": _load(row["body"]),
            "digest": row["digest"],
            "revoked": bool(row["revoked"]),
        }

    def revoke_intent(self, agent_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("UPDATE intents SET revoked = 1, updated_at = ? WHERE agent_id = ?", (_now(), agent_id))
            return cur.rowcount == 1

    def list_intents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT agent_id FROM intents ORDER BY agent_id").fetchall()
        return [i for i in (self.get_intent(r["agent_id"]) for r in rows) if i is not None]

    # -- settlement receipts -------------------------------------------------------------------------------

    def put_receipt(self, deal_id: str, body: dict[str, Any]) -> bool:
        """Keep the first receipt issued for a deal. Returns False when one already exists."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO receipts (deal_id, body, issued_at) VALUES (?, ?, ?)",
                (deal_id, _dump(body), _now()),
            )
            return cur.rowcount == 1

    def get_receipt(self, deal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM receipts WHERE deal_id = ?", (deal_id,)).fetchone()
        return None if row is None else dict(_load(row["body"]))

    # -- webhooks ------------------------------------------------------------

    def record_webhook(self, event_id: str, event_type: str, resource_id: str | None, payload: dict[str, Any]) -> bool:
        """Record a PayPal webhook delivery. Returns False when `event_id` was already recorded."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO webhook_events (event_id, event_type, resource_id, received_at, payload)"
                " VALUES (?, ?, ?, ?, ?)",
                (event_id, event_type, resource_id, _now(), _dump(payload)),
            )
            return cur.rowcount == 1

    def forget_webhook(self, event_id: str) -> None:
        """Un-record a delivery we could not act on, so PayPal's retry is processed instead of deduped."""
        with self._tx() as conn:
            conn.execute("DELETE FROM webhook_events WHERE event_id = ?", (event_id,))


def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "agent_id": row["agent_id"],
        "public_key": row["public_key"],
        "principal": row["principal"],
        "roles": _load(row["roles"]),
        "paypal_email": row["paypal_email"],
        "metadata": _load(row["metadata"]),
        "created_at": row["created_at"],
    }


def _deal_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "deal_id": row["deal_id"],
        "state": row["state"],
        "contract": _load(row["contract"]),
        "snapshot": _load(row["snapshot"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
