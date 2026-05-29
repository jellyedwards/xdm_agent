"""Daily usage quotas for the public demo.

Two layers:
  - per-session limits keyed by an opaque session key (judge bucket): a cap on
    the number of "runs" of each expensive kind per UTC day.
  - a single global daily ceiling across everyone — the wallet backstop that
    stops a surprise bill if the demo gets hammered.

Counters live in Firestore (collection `quota`, one doc per day+key, atomic
Increment) when STORAGE_BACKEND=firestore, else in process memory for local dev.
Quotas only bite when PUBLIC_MODE is on (see main.py); locally everything is
ungated.
"""

import os
import logging
from datetime import datetime, timezone

# Per-session daily caps by op kind. A "run" is a hunt.
DAILY_LIMITS = {
    "hunt": int(os.getenv("QUOTA_DAILY_HUNTS", "20")),
    "mindset": int(os.getenv("QUOTA_DAILY_MINDSETS", "20")),
}
# Total expensive ops across all sessions per day — the hard wallet ceiling.
DAILY_GLOBAL = int(os.getenv("QUOTA_DAILY_GLOBAL", "600"))

_mem = {}  # (day, key) -> count, dev fallback


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _db():
    # Reuse the Firestore client from the store when in firestore mode.
    try:
        from storage import get_store, STORAGE_BACKEND
        if STORAGE_BACKEND == "firestore":
            return get_store().db
    except Exception as exc:
        logging.warning(f"quota: firestore unavailable, using memory: {exc}")
    return None


def _doc_id(day, key):
    return f"{day}__{key}".replace("/", "_")


def _get(key):
    day = _today()
    db = _db()
    if db is None:
        return _mem.get((day, key), 0)
    snap = db.collection("quota").document(_doc_id(day, key)).get()
    return (snap.to_dict() or {}).get("count", 0) if snap.exists else 0


def _incr(key, amount=1):
    day = _today()
    db = _db()
    if db is None:
        k = (day, key)
        _mem[k] = _mem.get(k, 0) + amount
        return _mem[k]
    from google.cloud import firestore
    doc = db.collection("quota").document(_doc_id(day, key))
    doc.set({"count": firestore.Increment(amount), "day": day, "key": key}, merge=True)
    snap = doc.get()
    return (snap.to_dict() or {}).get("count", amount)


def remaining(session_key, kind):
    """Best-effort remaining runs of `kind` for this session today (read-only)."""
    limit = DAILY_LIMITS.get(kind, DAILY_LIMITS["hunt"])
    return max(0, limit - _get(f"sess:{session_key}:{kind}"))


def check_and_consume(session_key, kind, trusted=False):
    """Returns (ok, reason, remaining). Consumes one unit of `kind` when ok.

    trusted=True (signed-in real user) skips the per-session cap but still
    counts toward — and is blocked by — the global daily ceiling.
    """
    if _get("GLOBAL") >= DAILY_GLOBAL:
        return False, "the public demo has reached its daily budget — please try again tomorrow.", 0
    limit = DAILY_LIMITS.get(kind, DAILY_LIMITS["hunt"])
    if not trusted:
        if _get(f"sess:{session_key}:{kind}") >= limit:
            label = "runs" if kind == "hunt" else f"{kind}s"
            return False, f"you've used your {limit} {label} for today — thanks for trying xdm_agent!", 0
    _incr("GLOBAL")
    if trusted:
        return True, "", -1
    new_s = _incr(f"sess:{session_key}:{kind}")
    return True, "", max(0, limit - new_s)
