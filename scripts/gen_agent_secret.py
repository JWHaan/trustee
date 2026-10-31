"""Print a fresh identity secret for a hosted agent (AGENT_IDENTITY_SECRET).

    uv run python scripts/gen_agent_secret.py

The secret is 32 random bytes, urlsafe base64. Both of the agent's Ed25519 keys derive from it, so keep it in the
Astropods secret vault and nowhere else: not in git, not in a log, not in chat. It prints once and is not stored.
Make a separate one for Northwind and Tidewater.
"""

from __future__ import annotations

import base64
import secrets


def main() -> int:
    print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
