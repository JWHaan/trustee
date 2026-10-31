"""Trustee as a service: public deal API, A2A Agent Card, durable deal state."""

from .agent_card import build_agent_card
from .routes import build_router
from .service import DealService, ServiceError

__all__ = ["DealService", "ServiceError", "build_agent_card", "build_router"]
