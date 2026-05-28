# Publishing to design xdm

> How the agent gets a curated collection into a user's mood board on
> designxdm.com without the user ever seeing the word "token".

## What we're solving

Three concrete user flows:

1. **Live publish.** "I'm looking at this mindset's feed right now. Push it to a board on designxdm.com."
2. **Embedded use.** "I'm already inside design xdm; the agent panel is right here in the UI. Don't make me sign in twice."
3. **Autonomous publish.** "Every Sunday morning, run a hunt and publish the new finds as a fresh board. I'm not going to be sitting there to authorise it."

Pattern (3) is where the temptation to ask the user to "paste an API key" lives. We refuse that temptation. The user is not technical; the word "token" must never appear in the UI.

## The model — Connected Apps

This is the GitHub Apps / Slack Apps / Stripe Connect pattern, adapted to Firebase Auth. Three nouns:

- **Identity**: Firebase user (`uid`). Both apps point at the same Firebase project (`design-xdm`), so a single sign-in covers both. Patterns 1+2 collapse to "one user, one identity".
- **Installation**: a per-user-per-agent grant. Stored on `xdm_server`. Has `id`, `user_uid`, `agent_id`, `permissions` (currently just `["board.create"]`), `created_at`, `revoked_at?`. **The agent only ever sees the opaque `installation_id`.** The user can revoke from designxdm.com → Settings → Connected apps.
- **Mint token**: a server endpoint the agent calls to swap `(installation_id, agent_credential)` → short-lived (5 min) Firebase custom-token-equivalent that the agent uses to call user-scoped endpoints. The user is never in the loop.

The agent has its own service identity:
- `AGENT_CLIENT_ID = "xdm_agent_v1"` (well-known)
- `AGENT_CLIENT_SECRET` (long random string, lives in Secret Manager in prod, in `.env` in dev)

`xdm_server` knows the secret. When the agent calls `mint_token`, it signs the request (HMAC over timestamp + installation_id) so the server can verify the agent's identity.

## Authoritative flows

### Flow 1 — Live publish (signed-in user, click "Publish now")

```
[ user has the agent UI open at lens.example.com or wherever ]
[ already signed in via Firebase Web SDK ]

UI → agent: POST /publish/mindset/{id}
              Authorization: Bearer <user's Firebase ID JWT>

agent → server: POST /board_from_external_agent
                  Authorization: Bearer <forwarded user JWT>
                  body: { board_name, theme, rubric_text, images: [...] }

server: verify_token(JWT)  →  uid
        create board owned by uid
        store images (hot-linked, IOTY pattern)
        return { board_id, board_url }

agent → UI: { board_url }
UI shows "Published! View board ↗"
```

No installation involved. The user is right there, present in the browser, with a fresh ID JWT in hand.

### Flow 2 — Embedded mode

The agent UI is loaded as an `<iframe src="lens.example.com" ...>` inside design xdm. The parent page (`designxdm.com`) is already signed in. We pipe the auth in via `postMessage`.

```
parent (designxdm.com)  →  iframe (agent UI):
  postMessage({ kind: "auth", id_token, uid, email, refresh_after_seconds })

iframe receives, stores in memory, marks itself "embedded mode = true".
iframe never shows a sign-in button. Header reads "as <email>".
```

When the agent's iframe needs a fresh token (current one expiring), it asks the parent:

```
iframe → parent: postMessage({ kind: "refresh_token" })
parent → iframe: postMessage({ kind: "auth", id_token: <fresh> })
```

Origin-checked on both sides. The agent never sees the user's password or any cookie.

`POST /publish/...` behaves identically to Flow 1.

### Flow 3 — Autonomous publish (the one that mustn't say "token")

This is where Connected Apps earn their keep. The user-facing surface is one click inside the agent panel:

> **Auto-publish to design xdm**
> *Every Sunday morning, push the latest hunt's new finds as a fresh board.*
> `[ Connect to design xdm ]`

What that one click triggers — embedded mode is the smoothest, but a popup fallback works too:

**Embedded sub-flow** (preferred — user is already on designxdm.com):

```
iframe (user clicks Connect):
  postMessage({ kind: "request_install", agent: "xdm_agent_v1", scopes: ["board.create"] })

parent (designxdm.com):
  shows a small consent strip at the top:
    "Lens wants permission to create mood boards on your account.  [Allow] [Cancel]"

  on Allow:
    POST /api/installations/create
      Authorization: Bearer <user's JWT>
      body: { agent_id: "xdm_agent_v1", scopes: ["board.create"] }
    → { installation_id }

  postMessage({ kind: "install_complete", installation_id, agent: "xdm_agent_v1" })

iframe receives installation_id, sends it to its backend:
  POST /installations/{mindset_id}  body: { installation_id }
  agent stores on the Mindset record.

Auto-publish toggle becomes available. UI says "Connected. [Disconnect]".
```

**Popup sub-flow** (when not embedded — same outcome, different chrome):

```
agent UI opens window.open("https://designxdm.com/connect/agent?agent=xdm_agent_v1&scopes=board.create")
designxdm.com:
  if not signed in, show sign-in
  show the consent strip, same as embedded
  on Allow, runs the same /api/installations/create
  posts the installation_id back to the opener via postMessage and closes itself
```

Once installed, the autonomous publish runs entirely server-to-server:

```
[ Cloud Scheduler fires; agent's scheduled hunt completes ]

agent (background worker):
  POST {xdm_server}/api/agents/installations/{installation_id}/mint_token
    Authorization: AgentSig <HMAC of timestamp + installation_id, signed with AGENT_CLIENT_SECRET>
    headers: X-Agent-Id: xdm_agent_v1, X-Agent-Timestamp: <epoch>
  →
  { jwt, expires_in: 300 }

agent: POST {xdm_server}/board_from_external_agent
         Authorization: Bearer <minted jwt>
         body: { board_name, theme, rubric_text, images: [...] }

server: standard verify_token flow, creates board owned by installation.user_uid.
```

The user never enters a credential. They saw two buttons in their life: **Allow** (once), and **Disconnect** (if they ever want to).

### Why this composes #1 + #2 + #3 cleanly

- #1 (shared Firebase project) is the substrate. Without it, neither the user-token forwarding nor the consent UI can identify the user.
- #2 (embedded mode) is how the install happens *seamlessly* — the user is already signed in, the consent strip is two lines, postMessage moves the `installation_id` across the iframe boundary.
- #3 (autonomous) is the long-lived state created by that one consent click. It survives the user closing their browser.

The single insight: **the install step lives inside the embedded #2 flow**, so the non-technical user never leaves the UI they were already in, never sees a popup, never types anything.

## Data model

### On `xdm_agent`

`Mindset` already has `owner_uid`. New optional fields:

```python
class Mindset(BaseModel):
    ...
    publish_installation_id: Optional[str] = None  # design-xdm installation
    publish_schedule: Optional[Dict[str, Any]] = None
    # e.g. {"cron": "0 9 * * 0", "next_run": "...", "last_run": "..."}
```

### On `xdm_server`

New table (or Firestore collection) `agent_installations`:

```
id           uuid primary key
user_uid     text not null            -- the design xdm user
agent_id     text not null            -- "xdm_agent_v1"
scopes       text[] not null          -- ["board.create"]
created_at   timestamptz
revoked_at   timestamptz null
last_used_at timestamptz null
```

Boards created by an installation get an audit row:

```
board.created_via_installation_id  uuid null
board.created_via_agent_id         text null
```

So if the user later wonders "why is this board here", they can trace it.

## Endpoint reference

### On `xdm_server` (new — lives in `agent_integration.py`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/board_from_external_agent` | `Depends(verify_token)` | Create a board on the verified user's account. Body has board name, theme, rubric, images, optional `installation_id` for audit. |
| `POST` | `/api/installations/create` | `Depends(verify_token)` | User-authorised: create an installation linking the user to an agent with scopes. Returns `installation_id`. |
| `POST` | `/api/agents/installations/{id}/mint_token` | `Depends(verify_agent_signature)` | Agent-authorised (HMAC). Returns a 5-min JWT scoped to the installation's user. |
| `GET` | `/api/installations` | `Depends(verify_token)` | List the user's current installations (for the Connected-apps settings UI). |
| `DELETE` | `/api/installations/{id}` | `Depends(verify_token)` | Revoke. |

### On `xdm_agent` (extends `main.py`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/publish/mindset/{id}` | Bearer JWT | Live publish — forwards the user's JWT to `xdm_server`. |
| `POST` | `/installations/{mindset_id}` | Bearer JWT | Store the installation_id on the mindset (called by the embed bridge or popup). |
| `DELETE` | `/installations/{mindset_id}` | Bearer JWT | Disconnect — clears installation_id; also calls `xdm_server DELETE /api/installations/{id}`. |
| `POST` | `/publish/mindset/{id}/autonomous` | Agent's own scheduler / cron | Mints a token via the installation, then publishes. Not user-callable. |

## Security notes

- Forwarded JWTs are validated by `xdm_server` exactly as if the design xdm UI had called it. Same `verify_token`. No bypass path.
- `AGENT_CLIENT_SECRET` lives in Secret Manager in prod, never in client-visible code, never logged.
- Installation `mint_token` requests are HMAC-signed with timestamp + installation_id; the server rejects requests where the timestamp is more than 60s skewed.
- Minted JWTs are stamped with `aud: "xdm_server"`, `installation_id`, and short `exp` (5 minutes). The agent does not cache them across mindsets — fresh mint per publish.
- Revocation is immediate — `revoked_at` is checked on every `mint_token` call.
- Per-installation rate limit on `mint_token` (e.g. 60/hour) prevents a compromised agent from causing damage at scale.
- `installation_id` is opaque (UUID v4); not derivable from the user's identity.

## Implementation order

This is what I'll land, in commits, in this order:

1. **Tile description ellipsis fix** — done (`cd22cd3`).
2. **PUBLISH.md** — this doc.
3. **Firebase Web SDK on agent UI** — `ui/auth.js` module, sign-in/out, current user surfaced.
4. **Agent backend auth** — `verify_user` middleware lifted/mirrored from xdm_server; degrades to a single-user "dev" if no Firebase admin creds.
5. **Live-publish endpoint** — `POST /publish/mindset/{id}` forwards Bearer JWT to xdm_server.
6. **xdm_server/agent_integration.py** — `/board_from_external_agent` + the installation endpoints, standalone so John can wire it into `main.py` with two lines when ready.
7. **UI: per-mindset "Publish now" button** — uses the live token.
8. **Embed bridge (#2)** — `postMessage` listener; iframe-mode detection; suppress sign-in when embedded.
9. **Connect / installation flow (#3)** — `[Connect]` button in the agent UI; popup fallback for non-embedded mode; storing `installation_id` on the Mindset; Disconnect.
10. **Autonomous publish** — agent-side scheduler that mints a token via the installation and publishes.

Stages 2–7 give a fully working live-publish demo. Stages 8–10 turn it into the seamless "auto-publish from inside design xdm" story.

## Open questions for John

- **Agent client secret distribution**: I'll generate one in dev and put it in both `.env` files. For prod, fine to use Secret Manager?
- **Where to store installations on `xdm_server`**: Postgres (new table) or Firestore? Postgres is consistent with the existing schema; Firestore is consistent with where xdm_agent's mindsets eventually live. I'd default to Postgres for now since `xdm_server` already runs against it.
- **Permission scopes**: starting with `board.create`. Future: `board.read` (let the agent show user "your existing boards" in the publish dialog), `board.update` (refresh a board with new finds rather than always making a fresh one). Do you want any of those in v1?
- **Naming**: the user-visible label for the agent. "Lens"? Currently the repo is `xdm_agent` — fine for the connected-apps row?
