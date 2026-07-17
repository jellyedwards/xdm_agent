import os
import json
import base64
import logging
import threading
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
import httpx
import uvicorn
from dotenv import load_dotenv

# Firebase admin is optional in dev — agent degrades to a single-user
# "dev" identity when no service-account credentials are present.
try:
    import firebase_admin
    from firebase_admin import auth as fb_auth, credentials as fb_credentials
    _FIREBASE_AVAILABLE = True
except Exception:
    _FIREBASE_AVAILABLE = False

from storage import Mindset, Candidate, FeedbackEvent, Hunt, get_store, now_iso, new_id
from rubric import initialise_mindset_rubric, reflect_rubric, record_feedback, default_tactic_prefs
from agents import run_hunt, plan_hunt, build_adk_agents, build_dossier, initialise_mindset_full
from sources import SOURCE_REGISTRY, source_ready
import quota
import billing

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

PORT = int(os.getenv("PORT", "8080"))
XDM_SERVER_URL = os.getenv("XDM_SERVER_URL", "")
XDM_SERVER_TOKEN = os.getenv("XDM_SERVER_TOKEN", "")
A2A_SHARED_SECRET = os.getenv("A2A_SHARED_SECRET", "")
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "xdm_agent_v1")
AGENT_CLIENT_SECRET = os.getenv("AGENT_CLIENT_SECRET", "")
DEV_AUTH_ALLOW = os.getenv("DEV_AUTH_ALLOW", "1") == "1"  # accept unsigned requests as the dev user
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "0") == "1"  # gate expensive ops behind access code + quota
ACCESS_CODES = {c.strip() for c in os.getenv("JUDGE_ACCESS_CODES", "").split(",") if c.strip()}

# Optionally initialise firebase_admin. Same FIREBASE_SERVICE_ACCOUNT
# base64-encoded JSON pattern xdm_server uses.
_firebase_ready = False
if _FIREBASE_AVAILABLE and not firebase_admin._apps:
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if raw:
        try:
            key_json = json.loads(base64.b64decode(raw).decode("utf-8"))
            firebase_admin.initialize_app(fb_credentials.Certificate(key_json))
            _firebase_ready = True
            logging.info("firebase_admin initialised — real token verification on")
        except Exception as exc:
            logging.warning(f"firebase_admin init failed; falling back to dev auth: {exc}")
elif _FIREBASE_AVAILABLE:
    _firebase_ready = True


def verify_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Verify a Firebase ID JWT from the Authorization header.

    Returns a dict with at least `uid` and (usually) `email`.
    In dev (no firebase_admin creds + DEV_AUTH_ALLOW=1), accepts any
    request and returns a stable "dev" identity so the UI works.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        if DEV_AUTH_ALLOW and not _firebase_ready:
            return {"uid": "dev", "email": "dev@local"}
        raise HTTPException(401, "Authorization: Bearer <token> required")
    token = authorization.split(" ", 1)[1].strip()
    if not _firebase_ready:
        if DEV_AUTH_ALLOW:
            return {"uid": "dev", "email": "dev@local", "raw_token_prefix": token[:8]}
        raise HTTPException(500, "Firebase admin not initialised")
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as exc:
        logging.info(f"verify_user: {exc}")
        raise HTTPException(401, "Invalid authentication token")
    return decoded


def verify_user_optional(authorization: Optional[str] = Header(None)) -> Optional[Dict[str, Any]]:
    """Same as verify_user but returns None instead of 401 when missing."""
    if not authorization:
        return None
    try:
        return verify_user(authorization)
    except HTTPException:
        return None


def _session_key(request: Request, user: Optional[Dict[str, Any]]):
    """(quota key, trusted?). Signed-in real users are trusted (own bucket, no
    per-session cap); judges are keyed by their access code + browser session."""
    if user and user.get("uid") and user.get("uid") != "dev":
        return f"uid:{user['uid']}", True
    code = (request.headers.get("X-Access-Code") or "").strip()
    sid = (request.headers.get("X-Session-Id") or "").strip()
    base = sid or (request.client.host if request.client else "anon")
    return f"code:{code}:{base}", False


def effective_owner(request: Request, user: Optional[Dict[str, Any]]) -> str:
    """Stable owner id for the caller, signed-in or anonymous.

    Signed-in real users own their data under their Firebase uid. Anonymous
    visitors are isolated per-browser via the persistent X-Session-Id token the
    UI stores in localStorage and sends on every request — so each browser only
    ever sees the mindsets it created, without anyone having to log in."""
    if user and user.get("uid") and user.get("uid") != "dev":
        return user["uid"]
    sid = (request.headers.get("X-Session-Id") or "").strip()
    base = sid or (request.client.host if request.client else "anon")
    return f"anon:{base}"


def assert_owner(m, request: Request, user: Optional[Dict[str, Any]]):
    """404 if the mindset is owned by someone other than the caller. Legacy
    records with no owner stay readable (they predate per-user scoping)."""
    if m.owner_uid and m.owner_uid != effective_owner(request, user):
        raise HTTPException(404, "mindset not found")


def _bearer_token(request: Request) -> str:
    return (request.headers.get("Authorization") or "").split(" ", 1)[-1].strip()


def public_gate(kind: str, request: Request, user: Optional[Dict[str, Any]],
                idempotency_key: Optional[str] = None) -> Optional[int]:
    """Free-tier / billing gate for expensive ops when PUBLIC_MODE is on.

    Anyone gets a small free daily allowance per browser session. Signed-in
    users pay for hunts with Design XDM credits (mindset ops stay free);
    access codes (judges / demos) bypass both. Everything still counts toward
    the global daily ceiling. Returns the xdm_server ledger id when the op was
    paid with credits — so the caller can refund a failed run — else None.
    No-op for local/dev and trusted single-tenant deployments."""
    if not PUBLIC_MODE:
        return None
    key, trusted = _session_key(request, user)
    code = (request.headers.get("X-Access-Code") or "").strip()
    if bool(ACCESS_CODES) and code in ACCESS_CODES:
        ok, reason, _rem = quota.check_and_consume(key, kind, trusted=True)
        if not ok:
            raise HTTPException(429, reason)
        return None
    if trusted and kind == "hunt":
        ok, reason, _rem = quota.check_and_consume(key, kind, trusted=True)
        if not ok:
            raise HTTPException(429, reason)
        res = billing.charge(_bearer_token(request), kind, idempotency_key or new_id())
        return res.get("ledger_id")
    ok, reason, _rem = quota.check_and_consume(key, kind, trusted=trusted)
    if not ok:
        # 402 tells the UI the fix is signing in / topping up, not waiting.
        raise HTTPException(429 if trusted else 402, reason)
    return None

app = FastAPI(title="xdm_agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened in deploy
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateMindsetReq(BaseModel):
    name: str
    theme: str
    owner_uid: Optional[str] = None
    serendipity: float = 0.2


class FeedbackReq(BaseModel):
    mindset_id: str
    kind: str  # like|dislike|hide|direction
    image_id: Optional[str] = None
    direction_text: Optional[str] = None
    note: Optional[str] = None


class HuntReq(BaseModel):
    mindset_id: str
    budget: Optional[int] = None
    # When True (default) the planner may also use the newer (2026-06-29)
    # cultural/scientific sources; set False to restrict to the classic set.
    include_new_sources: bool = True


class A2ACurateReq(BaseModel):
    agent_id: str
    brief: str
    n: int = 10
    max_seconds: int = 60


class PublishToXdmReq(BaseModel):
    mindset_id: str
    user_token: str
    board_name: Optional[str] = None
    max_images: int = 30


@app.get("/")
def root():
    return RedirectResponse("/ui/index.html")


@app.get("/health")
def health():
    sids = list(SOURCE_REGISTRY.keys())
    readiness = {sid: source_ready(sid) for sid in sids}
    ready = [sid for sid, (ok, _) in readiness.items() if ok]
    unavailable = {sid: reason for sid, (ok, reason) in readiness.items() if not ok}
    return {
        "ok": True,
        "ts": now_iso(),
        "sources": sids,
        "ready": ready,
        "unavailable": unavailable,
    }


@app.get("/sources")
def sources_meta():
    out = {}
    for sid, meta in SOURCE_REGISTRY.items():
        ok, reason = source_ready(sid)
        out[sid] = {
            "display_name": meta.get("display_name", sid),
            "kind": meta.get("kind"),
            "license_default": meta.get("license_default"),
            "ready": ok,
            "unavailable_reason": reason,
        }
    return out


@app.get("/quota")
def get_quota(request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    if not PUBLIC_MODE:
        return {"public_mode": False}
    key, trusted = _session_key(request, user)
    code = (request.headers.get("X-Access-Code") or "").strip()
    bypass = bool(ACCESS_CODES) and code in ACCESS_CODES
    out = {
        "public_mode": True,
        "trusted": trusted,
        "has_access": True,  # the agent is public now; kept for older UI builds
        "hunts_remaining": -1 if (trusted or bypass) else quota.remaining(key, "hunt"),
        "mindsets_remaining": -1 if (trusted or bypass) else quota.remaining(key, "mindset"),
        "daily_hunts": quota.DAILY_LIMITS["hunt"],
    }
    if trusted and not bypass:
        b = billing.balance(_bearer_token(request))
        if b is not None:
            cost = max(1, int((b.get("costs") or {}).get("hunt") or 1))
            out["billing"] = {"balance": b.get("balance", 0), "hunt_cost": cost}
            out["hunts_remaining"] = b.get("balance", 0) // cost
    return out


@app.post("/mindset")
def create_mindset(req: CreateMindsetReq, request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    public_gate("mindset", request, user)
    store = get_store()
    # Owner comes from the caller's identity (signed-in uid or anon browser
    # token), never from the request body — clients can't claim another owner.
    m = Mindset(name=req.name, theme=req.theme, owner_uid=effective_owner(request, user), serendipity=req.serendipity, tactic_prefs=default_tactic_prefs())
    store.save_mindset(m)
    try:
        m = initialise_mindset_full(m)
    except Exception as exc:
        logging.exception(f"mindset init failed for {m.id}: {exc}")
    return m.model_dump()


@app.post("/mindset/{mindset_id}/dossier")
def refresh_dossier(mindset_id: str, request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    public_gate("mindset", request, user)
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    assert_owner(m, request, user)
    m.dossier = build_dossier(m.theme)
    m.dossier_ts = now_iso()
    store.save_mindset(m)
    return m.model_dump()


@app.get("/mindset/{mindset_id}")
def get_mindset(mindset_id: str, request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    m = get_store().get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    assert_owner(m, request, user)
    return m.model_dump()


@app.get("/mindset")
def list_mindsets(request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    # Always scoped to the caller — each browser/account sees only its own.
    owner_uid = effective_owner(request, user)
    store = get_store()
    out = []
    for m in store.list_mindsets(owner_uid=owner_uid):
        d = m.model_dump()
        # A few preview thumbnails for the home list — liked first, then
        # top-scored surfaced (list_candidates already sorts that way).
        cands = store.list_candidates(m.id, limit=20)
        previews = [c.thumbnail_url or c.image_url for c in cands
                    if c.status == "liked" or (c.status == "surfaced" and (c.judge_score or 0) >= 7.0)]
        d["preview_urls"] = previews[:4]
        out.append(d)
    return out


@app.get("/mindset/{mindset_id}/rubric_versions")
def list_versions(mindset_id: str):
    return [v.model_dump() for v in get_store().list_rubric_versions(mindset_id)]


@app.post("/mindset/{mindset_id}/reflect")
def reflect(mindset_id: str):
    m = reflect_rubric(mindset_id)
    return m.model_dump()


@app.post("/hunt")
def hunt(req: HuntReq, request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    store = get_store()
    m = store.get_mindset(req.mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    assert_owner(m, request, user)
    # Pre-create the Hunt record so we can return an id immediately and the
    # client can poll while the background thread updates the trace. Its id
    # doubles as the billing idempotency key.
    h = Hunt(mindset_id=req.mindset_id)
    ledger_id = public_gate("hunt", request, user, idempotency_key=h.id)
    h.status = "queued"
    h.trace.append({"t": now_iso(), "step": "queued"})
    store.save_hunt(h)

    def _run():
        try:
            run_hunt(req.mindset_id, budget=req.budget, hunt_id=h.id,
                     include_new_sources=req.include_new_sources)
        except Exception as exc:
            logging.exception(f"background hunt {h.id} failed: {exc}")
            if ledger_id:
                billing.refund(ledger_id, reason=f"hunt {h.id} failed")

    threading.Thread(target=_run, daemon=True).start()
    return {"hunt_id": h.id, "mindset_id": req.mindset_id, "status": h.status}


@app.get("/hunts/{mindset_id}")
def list_hunts(mindset_id: str, request: Request, limit: int = 20, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    m = get_store().get_mindset(mindset_id)
    if m:
        assert_owner(m, request, user)
    return [h.model_dump() for h in get_store().list_hunts(mindset_id, limit=limit)]


@app.get("/hunt/{mindset_id}/{hunt_id}")
def get_hunt(mindset_id: str, hunt_id: str):
    store = get_store()
    h = store.get_hunt(hunt_id, mindset_id)
    if not h:
        raise HTTPException(404, "hunt not found")
    return h.model_dump()


@app.get("/collection/{mindset_id}")
def collection(mindset_id: str, request: Request, status: Optional[str] = None, min_score: float = 7.0, limit: int = 200, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    m = get_store().get_mindset(mindset_id)
    if m:
        assert_owner(m, request, user)
    cs = get_store().list_candidates(mindset_id, status=status, limit=limit)
    if status is None:
        # default UI feed: liked always stays; otherwise surfaced + above threshold.
        cs = [c for c in cs if c.status == "liked" or (c.status == "surfaced" and (c.judge_score or 0) >= min_score)]
    # newest-retrieved first — a stable order that doesn't reshuffle when an
    # image is liked (sorting liked-to-top made the feed jump on every click).
    cs.sort(key=lambda c: c.found_at or "", reverse=True)
    # tag the latest hunt's items so the UI can show a "new" badge
    latest_hunt_id = None
    hunts = get_store().list_hunts(mindset_id, limit=1)
    if hunts:
        latest_hunt_id = hunts[0].id
    out = []
    for c in cs:
        d = c.model_dump()
        d["is_new"] = bool(latest_hunt_id and c.hunt_id == latest_hunt_id)
        out.append(d)
    return out


@app.post("/feedback")
def feedback(req: FeedbackReq, request: Request, user: Optional[Dict[str, Any]] = Depends(verify_user_optional)):
    m = get_store().get_mindset(req.mindset_id)
    if m:
        assert_owner(m, request, user)
    res = record_feedback(
        mindset_id=req.mindset_id,
        kind=req.kind,
        image_id=req.image_id,
        direction_text=req.direction_text,
        note=req.note,
    )
    return res


@app.post("/a2a/curate")
def a2a_curate(req: A2ACurateReq, x_a2a_token: Optional[str] = Header(None)):
    if A2A_SHARED_SECRET and x_a2a_token != A2A_SHARED_SECRET:
        raise HTTPException(401, "bad a2a token")
    # ephemeral mindset for the call; not persisted
    store = get_store()
    m = Mindset(name=f"a2a:{req.agent_id}", theme=req.brief, owner_uid=f"a2a:{req.agent_id}")
    store.save_mindset(m)
    initialise_mindset_rubric(m)
    h = run_hunt(m.id, budget=max(4, min(12, req.n // 2)))
    cs = get_store().list_candidates(m.id, status="surfaced", limit=req.n)
    return {
        "trace_id": h.id,
        "candidates": [
            {
                "image_url": c.image_url,
                "source_page_url": c.source_page_url,
                "source_id": c.source_id,
                "title": c.title,
                "creator": c.creator,
                "license_name": c.license_name,
                "license_url": c.license_url,
                "attribution": c.attribution,
                "judge_score": c.judge_score,
                "judge_reason": c.judge_reason,
            } for c in cs
        ],
    }


@app.post("/publish/design_xdm")
def publish_design_xdm_legacy(req: PublishToXdmReq):
    """Legacy endpoint kept for tests; new clients should use POST /publish/mindset/{id}."""
    return _publish_to_xdm_server(req.mindset_id, req.board_name, req.max_images, req.user_token)


class PublishMindsetReq(BaseModel):
    board_name: Optional[str] = None
    max_images: int = 60
    installation_id: Optional[str] = None  # for autonomous flow; ignored on live publish
    image_ids: Optional[List[str]] = None  # explicit selection from the UI; falls back to liked+surfaced


@app.post("/publish/mindset/{mindset_id}")
def publish_mindset(
    mindset_id: str,
    req: PublishMindsetReq,
    authorization: Optional[str] = Header(None),
    user: Dict[str, Any] = Depends(verify_user),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        # In dev mode verify_user has already populated `user` even without a header;
        # we just have no token to forward, which means xdm_server will reject. Surface that.
        raise HTTPException(401, "publishing requires a signed-in user")
    user_token = authorization.split(" ", 1)[1].strip()
    return _publish_to_xdm_server(mindset_id, req.board_name, req.max_images, user_token, installation_id=req.installation_id, image_ids=req.image_ids)


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
    m.updated_at = now_iso()
    store.save_mindset(m)
    return {"ok": True, "installation_id": req.installation_id}


@app.delete("/installations/{mindset_id}")
def unlink_installation(
    mindset_id: str,
    authorization: Optional[str] = Header(None),
    user: Dict[str, Any] = Depends(verify_user),
):
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    iid = m.publish_installation_id
    m.publish_installation_id = None
    m.publish_schedule = None
    m.updated_at = now_iso()
    store.save_mindset(m)
    # Best-effort revoke on xdm_server so the connected-apps row is cleared too.
    # Don't fail the unlink if the server is unreachable or already revoked.
    if iid and XDM_SERVER_URL and authorization and authorization.lower().startswith("bearer "):
        try:
            httpx.delete(
                f"{XDM_SERVER_URL.rstrip('/')}/api/installations/{iid}",
                headers={"Authorization": authorization},
                timeout=10.0,
            )
        except Exception as exc:
            logging.info(f"server revoke best-effort failed: {exc}")
    return {"ok": True}


def _publish_to_xdm_server(mindset_id: str, board_name: Optional[str], max_images: int, user_token: str, installation_id: Optional[str] = None, image_ids: Optional[List[str]] = None):
    if not XDM_SERVER_URL:
        raise HTTPException(501, "XDM_SERVER_URL not configured")
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    if image_ids:
        # Explicit selection from the UI — publish exactly these, in order.
        # Look up within the mindset (Firestore can't fetch a candidate by id alone).
        by_id = {c.id: c for c in store.list_candidates(mindset_id, limit=1000)}
        cs = [by_id[i] for i in image_ids[:max_images] if i in by_id]
    else:
        # Prefer liked images; fall back to all surfaced ones.
        liked = store.list_candidates(mindset_id, status="liked", limit=max_images)
        surfaced = store.list_candidates(mindset_id, status="surfaced", limit=max_images)
        cs = (liked + [c for c in surfaced if c.id not in {x.id for x in liked}])[:max_images]
    if not cs:
        raise HTTPException(400, "no images to publish — like some first, or run a hunt")
    payload = {
        "name": board_name or m.name,
        "theme": m.theme,
        "rubric": m.rubric_text,
        "installation_id": installation_id,
        "agent_id": AGENT_CLIENT_ID,
        "images": [
            {
                "image_url": c.image_url,
                "source_page_url": c.source_page_url,
                "source": c.source_id,
                "title": c.title,
                "creator": c.creator,
                "license_name": c.license_name,
                "license_url": c.license_url,
                "attribution": c.attribution,
                "judge_score": c.judge_score,
                "judge_reason": c.judge_reason,
            } for c in cs
        ],
    }
    target = f"{XDM_SERVER_URL.rstrip('/')}/board_from_external_agent"
    try:
        r = httpx.post(
            target,
            json=payload,
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        # DNS/connect/timeout — the server was never reached. Surface a clear 502
        # instead of an opaque 500 so the UI can tell the user it's a config issue.
        logging.warning(f"publish: could not reach design xdm at {target}: {exc!r}")
        raise HTTPException(502, f"could not reach design xdm server ({XDM_SERVER_URL}): {exc}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"design xdm responded {r.status_code}: {r.text[:300]}")
    return r.json()


@app.on_event("startup")
def on_startup():
    try:
        build_adk_agents()
        logging.info("ADK agents built")
    except Exception as exc:
        logging.warning(f"ADK agents not built (install google-adk?): {exc}")


# Static UI mount (ui/ folder). Optional — present after we build it.
# Dev: serve with no-cache so edits show up on plain reload, no hard-refresh dance.
class _NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

if os.path.isdir(os.path.join(os.path.dirname(__file__), "ui")):
    app.mount("/ui", _NoCacheStatic(directory=os.path.join(os.path.dirname(__file__), "ui"), html=True), name="ui")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=os.getenv("RELOAD", "0") == "1")
