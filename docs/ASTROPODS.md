# Hosting Northwind and Tidewater on Astropods

Northwind (buyer) and Tidewater (seller) can run as two separate hosted agents on [Astropods](https://docs.astropods.com).
Trustee stays on Render. The arena's "Three A2A agents" scenario then drives the hosted agents instead of the two it
mounts in-process. Everything here is opt-in: with none of the variables below set, nothing changes.

```
 Render: Trustee + arena  <--A2A/HTTPS-->  Astropods: Northwind   (asked to buy by the arena)
        ^                                   Astropods: Tidewater  (Trustee pushes deal updates to it)
        '--- Northwind and Tidewater find each other by Agent Card, sign every move with Ed25519
```

## What is in the repo

| Path | Purpose |
| --- | --- |
| `deploy/astropods/northwind/`, `deploy/astropods/tidewater/` | `astropods.yml` and `AGENT.md` for each agent |
| `deploy/astropods/Dockerfile` | one image recipe; each spec passes `AGENT=northwind` or `tidewater` |
| `src/trustee/network/hosted.py` | the container entry point, configured by environment variables |
| `src/trustee/network/trigger.py` | Northwind's operator purchase routes (off unless a secret is set) |
| `src/trustee/network/remote.py`, `arena/a2a_flow.py` | the arena side: remote mode |
| `scripts/gen_agent_secret.py` | makes an identity secret |

Both specs pass `ast spec validate` (ast 0.31.2). The Docker build and an actual deploy have not been run.

## Your steps

Run these from the repository root. `ast login` and the deploy are yours to run; nothing here does them for you.

1. **Install the CLI** (puts `ast` in `~/.ast/bin`): `curl -fsSL https://astropods.com/install | sh`, then `ast --version`.
2. **Log in:** `ast login`.
3. **Make the secrets.** One identity secret per agent, plus one shared trigger secret:
   ```bash
   uv run python scripts/gen_agent_secret.py     # Northwind identity
   uv run python scripts/gen_agent_secret.py     # Tidewater identity
   uv run python scripts/gen_agent_secret.py     # trigger secret (any long random string works)
   ```
   Store them in the vault (the CLI prompts for the value, so it stays out of your shell history):
   ```bash
   ast secrets create NORTHWIND_IDENTITY_SECRET
   ast secrets create TIDEWATER_IDENTITY_SECRET
   ast secrets create NORTHWIND_TRIGGER_SECRET
   ast secrets create TRUSTEE_OPERATOR_TOKEN        # only if Trustee on Render has an operator token
   ```
   Keep the trigger secret to paste into Render in step 8.
4. **Validate and push both blueprints:**
   ```bash
   ast spec validate -f deploy/astropods/northwind/astropods.yml
   ast spec validate -f deploy/astropods/tidewater/astropods.yml
   (cd deploy/astropods/tidewater && ast blueprint push)
   (cd deploy/astropods/northwind && ast blueprint push)
   ```
   `ast blueprint push` builds the image from the spec's Dockerfile, so Docker must be running. Add `--allow-dirty`
   if the working tree has uncommitted changes.
5. **Deploy Tidewater first.** Its public URL is needed by Northwind and by Render. The public URL is only known
   after the first deploy, so the first deploy needs a placeholder `AGENT_PUBLIC_URL`; deploy, read the hostname with
   `ast agent list`, then redeploy with the real value:
   ```bash
   ast blueprint deploy trustee-tidewater \
     --var AGENT_PUBLIC_URL=https://<tidewater-host> \
     --var TRUSTEE_URL=https://trustee-arena.onrender.com \
     --var AGENT_IDENTITY_SECRET=@TIDEWATER_IDENTITY_SECRET \
     --var TRUSTEE_OPERATOR_TOKEN=@TRUSTEE_OPERATOR_TOKEN \
     --var PAYPAL_SELLER_EMAIL=<your sandbox business account>
   ```
   Pick a gateway model (`claude-sonnet-4-6` or `claude-haiku-4-5`) when asked. Add `--var TIDEWATER_CHEAT=1` for the
   cheating-seller demo.
6. **Deploy Northwind** the same way:
   ```bash
   ast blueprint deploy trustee-northwind \
     --var AGENT_PUBLIC_URL=https://<northwind-host> \
     --var TRUSTEE_URL=https://trustee-arena.onrender.com \
     --var TIDEWATER_URL=https://<tidewater-host> \
     --var AGENT_IDENTITY_SECRET=@NORTHWIND_IDENTITY_SECRET \
     --var NORTHWIND_TRIGGER_SECRET=@NORTHWIND_TRIGGER_SECRET \
     --var TRUSTEE_OPERATOR_TOKEN=@TRUSTEE_OPERATOR_TOKEN \
     --var PAYPAL_BUYER_EMAIL=<your sandbox personal account>
   ```
   `--var KEY=@SECRET_NAME` resolves a vault secret at deploy time (see `ast blueprint deploy --help`).
7. **Make both agents' interface public.** An agent's own HTTP surface sits behind Astropods sign-in by default, and
   the A2A routes must be reachable without it (the agents authenticate each message by signature, and Trustee and
   the arena call them from outside). In the Astropods console, set each agent's interface to public. Then check:
   ```bash
   curl https://<tidewater-host>/.well-known/agent-card.json
   curl https://<northwind-host>/healthz
   ```
   A sign-in redirect or 401 means the interface is not public yet.
8. **Point Render at them.** On the Trustee service add (these are the only changes):
   ```
   NORTHWIND_URL=https://<northwind-host>
   TIDEWATER_URL=https://<tidewater-host>
   NORTHWIND_TRIGGER_SECRET=<the trigger secret from step 3>
   ```
   Render restarts, and the "Three A2A agents" scenario now runs against the hosted agents.

## What changes on Render

Only when all three of `NORTHWIND_URL`, `TIDEWATER_URL` and `NORTHWIND_TRIGGER_SECRET` are set, the `a2a` scenario
asks the hosted Northwind (`POST /operator/purchases`) to buy from the hosted Tidewater and follows it by polling.
The in-process agents under `/agents/...` stay mounted but are not used for that scenario. Other scenarios are
unaffected.

The tape in remote mode shows Northwind's own A2A calls (to Tidewater and to Trustee), the PayPal approval link, and
Trustee's rows for the deal. It does not show Tidewater's side (its replies, the push it receives, its delivery):
those hops happen on a server the arena cannot observe. The contract, escrow and ruling are still visible through
Trustee. The scenario's own price and cheat settings do not travel to the hosted agents: they play whatever they are
configured with (`TIDEWATER_CHEAT` on Tidewater).

## How the hosted agents are configured

| Variable | Meaning |
| --- | --- |
| `AGENT_PUBLIC_URL`, `TRUSTEE_URL` | required. The agent's own public URL (in its card) and Trustee's |
| `PORT`, `HOST` | default 80 and 0.0.0.0 (Astropods routes to port 80) |
| `AGENT_IDENTITY_SECRET` | keys and id derive from it, so they survive restarts. Unset: fresh keys each start |
| `TRUSTEE_OPERATOR_TOKEN` | Trustee's operator token, if set |
| `TRUSTEE_MODE` | `auto` (default; live when an LLM key or the AI Gateway exists), `live`, `scripted` |
| `TIDEWATER_CHEAT` | `1` ships the corrupt dataset |
| `TIDEWATER_URL` (Northwind) | the only seller the trigger will buy from |
| `NORTHWIND_TRIGGER_SECRET` | enables `POST /operator/purchases` and `GET /operator/purchases/{id}`; unset: 404 |

Model: with `LLM_BASE_URL` and `LLM_API_KEY` unset, `ASTRO_GATEWAY_URL` + `/v1`, `ASTRO_GATEWAY_API_KEY` and
`MODEL_DEFAULT` are used. Any explicit `LLM_*` variable wins, and `LLM_MODEL` beats `MODEL_DEFAULT`.

A Trustee agent record is kept for good, keyed by agent id and public key. A stable identity registers once and is a
no-op afterwards; a lost or changed `AGENT_IDENTITY_SECRET` produces a new id.

## Roll back

Unset `NORTHWIND_URL`, `TIDEWATER_URL` and `NORTHWIND_TRIGGER_SECRET` on Render. The arena goes back to its in-process
agents at the next restart. The hosted agents can then be left idle or removed with `ast`.
