"""Client for xdm_server's agent billing endpoints (charge / refund / balance).

Signed-in hunts are paid with Design XDM credits. The agent proves its own
identity with the HMAC scheme from PUBLISH.md (X-Agent-Id + timestamp +
AgentSig Authorization) and proves the *user* initiated the op by forwarding
the user's Firebase ID token in X-User-Token — xdm_server verifies both, so
the agent can never charge a user who didn't just call it. Pricing lives on
xdm_server; this client never states an amount.

Env (shared with the entry in xdm_server's AGENT_CLIENT_SECRETS):

    XDM_SERVER_URL=https://api.designxdm.com
    AGENT_CLIENT_ID=xdm_agent_v1
    AGENT_CLIENT_SECRET=<long-random-secret>
"""

import os
import hmac
import time
import hashlib
import logging

import httpx
from fastapi import HTTPException

XDM_SERVER_URL = os.getenv("XDM_SERVER_URL", "").rstrip("/")
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "xdm_agent_v1")
AGENT_CLIENT_SECRET = os.getenv("AGENT_CLIENT_SECRET", "")


def enabled() -> bool:
    return bool(XDM_SERVER_URL and AGENT_CLIENT_SECRET)


def _agent_headers():
    ts = str(int(time.time()))
    sig = hmac.new(AGENT_CLIENT_SECRET.encode(), f"{AGENT_CLIENT_ID}.{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Agent-Id": AGENT_CLIENT_ID, "X-Agent-Timestamp": ts, "Authorization": f"AgentSig {sig}"}


def charge(user_token: str, kind: str, idempotency_key: str) -> dict:
    """Deduct the op's cost from the signed-in user's credit balance.

    Returns {ledger_id, cost, balance}. Raises HTTPException 402 when the
    balance is short and 503 when billing can't be reached — paid ops fail
    closed; the anonymous free tier never touches this path.
    """
    if not enabled():
        logging.error("billing: XDM_SERVER_URL / AGENT_CLIENT_SECRET not configured")
        raise HTTPException(503, "billing is not configured — please try again later.")
    try:
        r = httpx.post(
            f"{XDM_SERVER_URL}/api/agents/charge",
            json={"kind": kind, "idempotency_key": idempotency_key},
            headers={**_agent_headers(), "X-User-Token": user_token},
            timeout=10.0,
        )
    except Exception as exc:
        logging.warning(f"billing: charge unreachable: {exc}")
        raise HTTPException(503, "billing is temporarily unavailable — please try again shortly.")
    if r.status_code == 402:
        raise HTTPException(402, "you're out of Design XDM credits — top up to keep hunting.")
    if r.status_code == 401:
        # 401 is either the user's expired token or our own bad agent
        # signature — only the former is the user's problem to fix.
        detail = ""
        try:
            detail = r.json().get("detail", "")
        except Exception:
            pass
        if "user token" in detail:
            raise HTTPException(401, "your session has expired — please sign in again.")
        logging.error(f"billing: agent auth rejected by xdm_server: {detail}")
        raise HTTPException(503, "billing is temporarily unavailable — please try again shortly.")
    if r.status_code != 200:
        logging.warning(f"billing: charge failed {r.status_code}: {r.text[:200]}")
        raise HTTPException(503, "billing is temporarily unavailable — please try again shortly.")
    return r.json()


def refund(ledger_id: int, reason: str = ""):
    """Best-effort refund after a failed hunt. Never raises — a refund that
    can't go through is logged for manual reconciliation, not surfaced."""
    try:
        r = httpx.post(
            f"{XDM_SERVER_URL}/api/agents/refund",
            json={"ledger_id": ledger_id},
            headers=_agent_headers(),
            timeout=10.0,
        )
        if r.status_code != 200:
            logging.error(f"billing: refund of ledger {ledger_id} failed {r.status_code}: {r.text[:200]} ({reason})")
    except Exception as exc:
        logging.error(f"billing: refund of ledger {ledger_id} unreachable: {exc} ({reason})")


def balance(user_token: str):
    """Best-effort {balance, costs} for the signed-in user; None if unavailable."""
    if not enabled():
        return None
    try:
        r = httpx.get(
            f"{XDM_SERVER_URL}/api/agents/balance",
            headers={**_agent_headers(), "X-User-Token": user_token},
            timeout=10.0,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as exc:
        logging.info(f"billing: balance unavailable: {exc}")
        return None
