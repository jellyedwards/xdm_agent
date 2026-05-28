# NEXT — pick up here tomorrow

> Continues from [PUBLISH.md](./PUBLISH.md) §"Implementation order".
> Stages 1–7 are landed. This doc walks through 8, 9, 10 in the order
> they should actually be built, with concrete files, code sketches and
> acceptance criteria.

---

## State right now (end of today)

**Committed on `main` (xdm_agent):**

```
4dd6b45  Firebase Web SDK auth + live-publish endpoint
34811c5  Add PUBLISH.md: Connected Apps model for design xdm integration
cd22cd3  Truncate tile judge_reason + title with ellipsis
5a3722c  Hunt progress card takes the full feed row width
b739ae1  Dossier polish: cap previews to 3 + …, wrap expanded body, drop sources badge
93b509e  Honest sources-online indicator: show ready / total with reasons
a84c454  Collapse dossier subsections with concise previews
a4b3dee  Adopt ClashDisplay as the primary UI font
dbafec3  UI re-skin to match xdm_client visual language
806d31a  Initial commit: xdm_agent — autonomous taste-learning visual curator
```

**Sitting in `xdm_server/` working tree, uncommitted:**

- `agent_integration.py` — fully written, expects two lines added to `main.py` to wire it in. Doc-comment at the top documents the exact wiring and the SQL migration needed.

**What's running locally:**

- uvicorn on `:8080` (may not survive overnight; just restart)
- `xdm_agent/.local_store.json` keeps mindsets + candidates across restarts
- The mindset `6d491fb2-00fa-42f4-be8f-da2193d9a857` has the microscopy data + likes used in yesterday's tests

---

## Pre-flight (~15 min, must be done first)

These three tasks unblock everything else. Do them in order.

### P1 — Wire `agent_integration.py` into xdm_server

In `xdm_server/main.py`, near the bottom (after the existing routes):

```python
from agent_integration import setup_agent_routes
setup_agent_routes(app)
```

### P2 — Schema migration

Run once against the same Postgres that xdm_server uses. Either as a new migration file or one-shot:

```sql
CREATE TABLE IF NOT EXISTS agent_installations (
    id            uuid PRIMARY KEY,
    user_uid      text NOT NULL,
    agent_id      text NOT NULL,
    scopes        text[] NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz,
    last_used_at  timestamptz
);
CREATE INDEX IF NOT EXISTS agent_installations_user
    ON agent_installations (user_uid)
    WHERE revoked_at IS NULL;

ALTER TABLE boards
    ADD COLUMN IF NOT EXISTS created_via_agent_id text,
    ADD COLUMN IF NOT EXISTS created_via_installation_id uuid;
```

### P3 — Secrets

Generate the shared agent secret once:

```sh
openssl rand -hex 32
```

Put it on both sides:

```sh
# xdm_agent/.env
AGENT_CLIENT_ID=xdm_agent_v1
AGENT_CLIENT_SECRET=<the secret>

# xdm_server/.env
AGENT_CLIENT_SECRETS=xdm_agent_v1:<the secret>
```

Also drop `FIREBASE_SERVICE_ACCOUNT=<same base64 xdm_server uses>` into `xdm_agent/.env` so the agent verifies real tokens instead of the dev-mode fallback.

### P4 — Smoke test the wiring

With both servers restarted:

```sh
# xdm_server should now expose:
curl -s -i http://localhost:<xdm-server-port>/board_from_external_agent | head -1
# → 405 Method Not Allowed (means the route is registered)

# xdm_agent live publish should now work end-to-end:
# open http://127.0.0.1:8080/ui/index.html, sign in, hit "publish to design xdm"
# expect a real board URL back
```

---

## Stage 9 — Connect / Disconnect UI

Build first because it produces the `installation_id` that stage 10 depends on. Works fine outside of embedded mode via a popup, so it's testable in isolation.

### What it does

On each mindset page, a third block of buttons appears below the nudge card:

```
┌──────────────────────────────────────────────────────────┐
│  AUTO-PUBLISH                                            │
│                                                          │
│  Not connected.                                          │
│  [ Connect to design xdm ]                               │
│                                                          │
│  (after connect:)                                        │
│  Connected.  Last published: never                       │
│  Schedule: [ off ] [ daily ] [ weekly ]                  │
│  [ Disconnect ]                                          │
└──────────────────────────────────────────────────────────┘
```

`Connect` button behaviour depends on `window.agentAuth.isEmbedded`:

- **Embedded:** call `window.agentAuth.requestInstall(agentId, scopes)`, which posts `{kind:'request_install'}` to the parent (stage 8 builds the parent listener) and awaits the install completion event.
- **Standalone:** open a popup at `https://designxdm.com/connect/agent?agent=xdm_agent_v1&scopes=board.create&return_origin=<this-origin>`. The popup handles sign-in (if needed), shows the consent strip, calls `POST /api/installations/create`, posts `{kind:'install_complete', installation_id}` back to `window.opener`, closes itself.

### Files to touch

| File | Change |
|---|---|
| `xdm_agent/storage.py` | Add `publish_installation_id: Optional[str]` and `publish_schedule: Optional[Dict]` to the `Mindset` model |
| `xdm_agent/main.py` | Two new endpoints (see below) |
| `xdm_agent/ui/app.js` | New `renderAutoPublish(id, m)` function; new `connectInstallation(id)` and `disconnectInstallation(id)` handlers; listen for the `agent-install-complete` window event from `auth.js` |
| `xdm_agent/ui/auth.js` | Add a popup-mode helper that opens the connect URL and listens for `postMessage` from the opener |
| `xdm_client` | New page `/connect/agent` that does the consent UI + the install POST + the postMessage. **This change lives in your repo, not mine.** |
| `xdm_server/agent_integration.py` | Already has `POST /api/installations/create` and `DELETE /api/installations/{id}`. Nothing to add here. |

### `xdm_agent/main.py` additions

```python
class InstallationLinkReq(BaseModel):
    installation_id: str

@app.post("/installations/{mindset_id}")
def link_installation(
    mindset_id: str,
    req: InstallationLinkReq,
    user: Dict[str, Any] = Depends(verify_user),
):
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    m.publish_installation_id = req.installation_id
    store.save_mindset(m)
    return {"ok": True, "installation_id": req.installation_id}

@app.delete("/installations/{mindset_id}")
def unlink_installation(
    mindset_id: str,
    user: Dict[str, Any] = Depends(verify_user),
    authorization: Optional[str] = Header(None),
):
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    iid = m.publish_installation_id
    m.publish_installation_id = None
    m.publish_schedule = None
    store.save_mindset(m)
    # Best-effort revoke on the server. Don't fail if it's already gone.
    if iid and XDM_SERVER_URL and authorization:
        try:
            httpx.delete(
                f"{XDM_SERVER_URL.rstrip('/')}/api/installations/{iid}",
                headers={"Authorization": authorization},
                timeout=10.0,
            )
        except Exception as exc:
            logging.info(f"server revoke best-effort failed: {exc}")
    return {"ok": True}
```

### Schedule field (placeholder for stage 10)

```python
class ScheduleReq(BaseModel):
    cron: Optional[str] = None  # None → off

@app.post("/mindset/{mindset_id}/schedule")
def set_schedule(mindset_id: str, req: ScheduleReq, user: Dict[str, Any] = Depends(verify_user)):
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    m.publish_schedule = {"cron": req.cron, "last_run": None, "next_run": None}
    store.save_mindset(m)
    return m.publish_schedule
```

### `ui/auth.js` popup helper

Add to the bottom of `window.agentAuth`:

```js
connectViaPopup: async (agentId, scopes) => {
  const url = `https://designxdm.com/connect/agent?agent=${encodeURIComponent(agentId)}` +
              `&scopes=${encodeURIComponent(scopes.join(","))}` +
              `&return_origin=${encodeURIComponent(location.origin)}`;
  const w = window.open(url, "xdm-connect", "width=500,height=720");
  return new Promise((resolve, reject) => {
    const tStart = Date.now();
    const h = (ev) => {
      if (!TRUSTED_PARENT_ORIGINS.has(ev.origin)) return;
      const m = ev.data;
      if (m?.kind === "install_complete" && m.installation_id) {
        window.removeEventListener("message", h);
        try { w.close(); } catch {}
        resolve(m);
      } else if (m?.kind === "install_cancelled") {
        window.removeEventListener("message", h);
        try { w.close(); } catch {}
        reject(new Error("cancelled"));
      }
    };
    window.addEventListener("message", h);
    // gentle timeout in case the popup is closed silently
    const poll = setInterval(() => {
      if (w.closed || Date.now() - tStart > 300_000) {
        clearInterval(poll);
        window.removeEventListener("message", h);
        reject(new Error("popup closed"));
      }
    }, 1000);
  });
},
```

### `xdm_client/src/app/connect/agent/page.tsx` (your repo)

Minimal sketch — the actual page should use the existing Material UI components for consistency:

```tsx
// app/connect/agent/page.tsx
"use client";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth } from "@/app/components/login/useAuth";  // or wherever it lives

export default function ConnectAgent() {
  const params = useSearchParams();
  const agent = params.get("agent") || "";
  const scopes = (params.get("scopes") || "").split(",").filter(Boolean);
  const returnOrigin = params.get("return_origin") || "";
  const { user, idToken, signIn } = useAuth();
  const [busy, setBusy] = useState(false);

  async function allow() {
    setBusy(true);
    const r = await fetch("/api/installations/create", {
      method: "POST",
      headers: { "content-type": "application/json", "Authorization": `Bearer ${idToken}` },
      body: JSON.stringify({ agent_id: agent, scopes }),
    });
    const data = await r.json();
    window.opener?.postMessage({ kind: "install_complete", installation_id: data.installation_id, agent }, returnOrigin);
    window.close();
  }
  function cancel() {
    window.opener?.postMessage({ kind: "install_cancelled" }, returnOrigin);
    window.close();
  }

  if (!user) return <div><button onClick={signIn}>Sign in to continue</button></div>;
  return (
    <div style={{ padding: 32, maxWidth: 480, fontFamily: "ClashDisplay, sans-serif" }}>
      <h2>Connect {agent.replace("_", " ")}</h2>
      <p><b>{agent}</b> wants permission to:</p>
      <ul>{scopes.map(s => <li key={s}>{s === "board.create" ? "Create mood boards on your account" : s}</li>)}</ul>
      <p style={{ color: "#757575" }}>You can disconnect any time from your account settings.</p>
      <div style={{ display: "flex", gap: 8 }}>
        <button onClick={allow} disabled={busy}>Allow</button>
        <button onClick={cancel}>Cancel</button>
      </div>
    </div>
  );
}
```

### Acceptance

- Click `Connect to design xdm` in the agent panel → consent popup opens at designxdm.com → click Allow → popup closes → button row reads `Connected.` and `[ Disconnect ]` appears.
- `GET /mindset/{id}` returns `publish_installation_id` non-null.
- On `xdm_server`, `SELECT * FROM agent_installations WHERE user_uid = <uid>` shows the row.
- Click Disconnect → row's `revoked_at` is set; `publish_installation_id` cleared on the mindset.

---

## Stage 10 — Autonomous publish

### What it does

If a mindset has both `publish_installation_id` and `publish_schedule.cron`, a background job evaluates the cron periodically and, when it fires:

1. Runs a hunt (existing `run_hunt`).
2. Mints a short-lived JWT via `POST /api/agents/installations/{id}/mint_token` on xdm_server, signed with the agent's HMAC.
3. Exchanges the custom token for a real ID token via Firebase's `securetoken.googleapis.com:signInWithCustomToken` REST endpoint.
4. Publishes the kept set as a fresh board (or appends — see open question below) via `/board_from_external_agent`.
5. Updates `last_run` and `next_run` on the mindset's schedule.

### Files to touch

| File | Change |
|---|---|
| `xdm_agent/main.py` | `mint_installation_token(installation_id)` helper; `autonomous_publish(mindset_id)` function; background scheduler thread on startup |
| `xdm_agent/requirements.txt` | add `croniter` for cron parsing |

### `mint_installation_token` sketch

```python
import hmac, hashlib, time

def _agent_sig(ts: int) -> str:
    msg = f"{AGENT_CLIENT_ID}.{ts}".encode()
    return hmac.new(AGENT_CLIENT_SECRET.encode(), msg, hashlib.sha256).hexdigest()

def mint_installation_token(installation_id: str) -> str:
    """Returns a Firebase ID token usable as Bearer for xdm_server."""
    ts = int(time.time())
    r = httpx.post(
        f"{XDM_SERVER_URL.rstrip('/')}/api/agents/installations/{installation_id}/mint_token",
        headers={
            "Authorization": f"AgentSig {_agent_sig(ts)}",
            "X-Agent-Id": AGENT_CLIENT_ID,
            "X-Agent-Timestamp": str(ts),
        },
        timeout=15.0,
    )
    r.raise_for_status()
    custom_token = r.json()["custom_token"]

    # Exchange custom token for ID token via Firebase REST
    # https://firebase.google.com/docs/reference/rest/auth#section-verify-custom-token
    api_key = os.environ["FIREBASE_WEB_API_KEY"]  # public Web SDK API key
    r = httpx.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}",
        json={"token": custom_token, "returnSecureToken": True},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()["idToken"]
```

Add `FIREBASE_WEB_API_KEY=AIzaSyDggOfJzx7KtpoO1nFcRaty58yriL0HBFM` (same as in `auth.js`) to `.env.example` and `.env`.

### `autonomous_publish` sketch

```python
def autonomous_publish(mindset_id: str):
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m or not m.publish_installation_id:
        return
    h = run_hunt(mindset_id)            # synchronous; uses scheduler thread anyway
    if h.n_kept == 0:
        logging.info(f"autonomous_publish: nothing new for {mindset_id}")
        return
    id_token = mint_installation_token(m.publish_installation_id)
    res = _publish_to_xdm_server(
        mindset_id,
        board_name=f"{m.name} — {datetime.now().strftime('%b %-d')}",
        max_images=60,
        user_token=id_token,
        installation_id=m.publish_installation_id,
    )
    m.publish_schedule["last_run"] = now_iso()
    store.save_mindset(m)
    logging.info(f"autonomous_publish: {mindset_id} -> {res.get('board_url')}")
```

### Background scheduler

A single background thread on app startup that loops every 60s, walks every mindset with a `publish_schedule.cron`, and uses `croniter` to decide whether the cron has elapsed since `last_run`.

```python
from croniter import croniter

def _scheduler_loop():
    while True:
        try:
            for m in get_store().list_mindsets():
                sched = m.publish_schedule or {}
                cron = sched.get("cron")
                if not cron: continue
                last = sched.get("last_run") or m.created_at
                ci = croniter(cron, datetime.fromisoformat(last))
                next_at = ci.get_next(datetime)
                if next_at <= datetime.now(timezone.utc):
                    autonomous_publish(m.id)
        except Exception as exc:
            logging.exception(f"scheduler: {exc}")
        time.sleep(60)

@app.on_event("startup")
def _start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()
```

(For Cloud Run prod, swap the in-process thread for Cloud Scheduler hitting a `/cron/tick` endpoint — but the in-process version is fine for the demo and for dev.)

### Acceptance

- Set a mindset's schedule to a cron that fires in ~2 minutes (e.g. `*/2 * * * *`).
- Wait. Observe logs: `autonomous_publish: <id> -> https://designxdm.com/board/...`.
- Refresh design xdm — the new board is there, owned by the right user, with the agent's images.
- `SELECT last_used_at FROM agent_installations WHERE id = ...` is updated.

---

## Stage 8 — Embed bridge for design xdm

The visible payoff: open design xdm, the agent panel is right there in the UI, click Allow once, it's connected. No popup, no second tab.

### What it does

design xdm hosts the agent UI as an iframe (e.g. on a `/lab/curator` page). The parent page:

1. Posts `{kind:'auth', id_token, user, expires_in_seconds}` to the iframe on load, and again whenever the iframe asks via `{kind:'refresh_token'}`.
2. Listens for `{kind:'request_install'}` from the iframe; when received, shows a small inline consent strip (no popup), then calls `POST /api/installations/create` with the user's session, and posts `{kind:'install_complete', installation_id, agent}` back into the iframe.

### Files to touch

| File | Change |
|---|---|
| `xdm_client/src/app/lab/curator/page.tsx` | New page with the iframe |
| `xdm_client/src/app/lab/curator/EmbedBridge.tsx` | The auth + install message bridge |
| `xdm_agent/ui/auth.js` | Already has the embedded-mode listener — verify origins include the prod design xdm domain |
| `xdm_agent/ui/app.js` | When `isEmbedded`, prefer `requestInstall` over `connectViaPopup` (already wired conditionally) |

### EmbedBridge sketch

```tsx
"use client";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/app/components/login/useAuth";

const AGENT_ORIGIN = process.env.NEXT_PUBLIC_AGENT_ORIGIN || "https://lens.designxdm.com";

export default function EmbedBridge() {
  const { user, idToken } = useAuth();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [consent, setConsent] = useState<{ agent: string; scopes: string[] } | null>(null);

  // Pipe auth into the iframe whenever we have a token.
  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe || !idToken || !user) return;
    function send() {
      iframe!.contentWindow?.postMessage({
        kind: "auth",
        id_token: idToken,
        expires_in_seconds: 3300,
        user: { uid: user.uid, email: user.email, display_name: user.displayName },
      }, AGENT_ORIGIN);
    }
    iframe.addEventListener("load", send);
    send();  // for already-loaded iframes
    return () => iframe.removeEventListener("load", send);
  }, [idToken, user]);

  // Handle messages from the iframe.
  useEffect(() => {
    async function handler(ev: MessageEvent) {
      if (ev.origin !== AGENT_ORIGIN) return;
      const m = ev.data;
      if (m?.kind === "refresh_token" && idToken) {
        // Force a fresh token, then re-pipe.
        const fresh = await user!.getIdToken(true);
        iframeRef.current?.contentWindow?.postMessage({ kind: "auth", id_token: fresh, expires_in_seconds: 3300 }, AGENT_ORIGIN);
      } else if (m?.kind === "request_install") {
        setConsent({ agent: m.agent, scopes: m.scopes });
      }
    }
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [idToken, user]);

  async function allowInstall() {
    if (!consent || !idToken) return;
    const r = await fetch("/api/installations/create", {
      method: "POST",
      headers: { "content-type": "application/json", "Authorization": `Bearer ${idToken}` },
      body: JSON.stringify({ agent_id: consent.agent, scopes: consent.scopes }),
    });
    const data = await r.json();
    iframeRef.current?.contentWindow?.postMessage(
      { kind: "install_complete", installation_id: data.installation_id, agent: consent.agent },
      AGENT_ORIGIN
    );
    setConsent(null);
  }

  return (
    <div>
      {consent && (
        <div style={{ background: "#fff8d6", padding: 12, display: "flex", gap: 12 }}>
          <span><b>{consent.agent}</b> wants permission to create mood boards on your account.</span>
          <button onClick={allowInstall}>Allow</button>
          <button onClick={() => setConsent(null)}>Cancel</button>
        </div>
      )}
      <iframe
        ref={iframeRef}
        src={AGENT_ORIGIN}
        style={{ width: "100%", height: "calc(100vh - 80px)", border: 0 }}
      />
    </div>
  );
}
```

### Acceptance

- Navigate to `designxdm.com/lab/curator` while signed in.
- Agent panel loads inside design xdm. Header shows `as <your email>` (embedded-mode signal) and no sign-in button.
- Click `Connect to design xdm` inside the panel → consent strip slides down at the top of the design xdm page (not a popup). Click Allow → consent strip disappears, panel reads `Connected.`
- Same `agent_installations` row created as in stage 9.

---

## Decisions to lock in tomorrow

(Open questions from `PUBLISH.md`. Answer once and move on.)

1. **Storage for installations**: Postgres. _(Default unless changed — already what `agent_integration.py` assumes.)_
2. **Scopes**: just `board.create` for v1. `board.update` / `board.read` later. _Confirm or adjust the whitelist in `agent_integration.py`._
3. **Agent display name** in the connected-apps row: `xdm_agent` (technical), or pick something user-facing like `Lens`? _Affects the consent strip and the `agent_id` constant._
4. **Auto-publish behaviour**: new board every run, or update the same board (refreshing its image set)? _Affects whether `/board_from_external_agent` takes an `existing_board_id` parameter._

---

## Demo script for end of tomorrow

If everything above lands, this is the 90-second flow worth recording:

1. **Sign in** to the agent at lens.localhost (or whatever the embed host is).
2. **Create a mindset** for "brutalist concrete". Dossier builds.
3. **Hunt now**. Live trace fills in. ~10 images surface.
4. **Like 3** of them.
5. **Publish now** → new tab opens at `designxdm.com/board/<id>` with the 3 liked images + their attribution. Stamp on the board: "Created by xdm_agent".
6. **Inside design xdm**, navigate to `/lab/curator`. Agent panel embedded. Click `Connect to design xdm`. Consent strip → Allow. Connected.
7. **Set schedule** to "daily, 6am".
8. **Disconnect** — show that revocation also clears the row in `agent_installations` and that the next scheduled run silently no-ops.

That demo covers stages 7, 9, 8, 10 in sequence and is what the Devpost submission video should look like.

---

## Out of scope tomorrow (defer to D7+)

- Scheduled hunts that don't publish (already on `BLOCKERS.md` as a separate strand).
- "Find more like this" per-tile button — nice product idea but not on the publish-flow critical path.
- A2A endpoint for external agents (`POST /a2a/curate` already exists; full demo and writeup happens later).
- Real Cloud Scheduler topology — keep the in-process thread until Cloud Run deploy.
