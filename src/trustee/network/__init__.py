"""Northwind and Tidewater as real A2A agents: the A2A network.

Each agent has its own Agent Card and JSON-RPC endpoint, finds the others by their cards, and completes a deal
over A2A with Trustee holding the money. See `docs/A2A.md` (the network section) and `examples/a2a_network.py`.
"""

from __future__ import annotations

from .app import mount_agents
from .common import Hop, Waiter
from .northwind import Northwind, Purchase, PurchaseError, create_northwind
from .tidewater import Tidewater, create_tidewater

__all__ = [
    "Hop",
    "Northwind",
    "Purchase",
    "PurchaseError",
    "Tidewater",
    "Waiter",
    "create_northwind",
    "create_tidewater",
    "mount_agents",
]
