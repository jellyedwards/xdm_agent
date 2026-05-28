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

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

PORT = int(os.getenv("PORT", "8080"))
XDM_SERVER_URL = os.getenv("XDM_SERVER_URL", "")
XDM_SERVER_TOKEN = os.getenv("XDM_SERVER_TOKEN", "")
A2A_SHARED_SECRET = os.getenv("A2A_SHARED_SECRET", "")
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "xdm_agent_v1")
AGENT_CLIENT_SECRET = os.getenv("AGENT_CLIENT_SECRET", "")
DEV_AUTH_ALLOW = os.getenv("DEV_AUTH_ALLOW", "1") == "1"  # accept unsigned requests as the dev user

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


@app.post("/mindset")
def create_mindset(req: CreateMindsetReq):
    store = get_store()
    m = Mindset(name=req.name, theme=req.theme, owner_uid=req.owner_uid, serendipity=req.serendipity, tactic_prefs=default_tactic_prefs())
    store.save_mindset(m)
    try:
        m = initialise_mindset_full(m)
    except Exception as exc:
        logging.exception(f"mindset init failed for {m.id}: {exc}")
    return m.model_dump()


@app.post("/mindset/{mindset_id}/dossier")
def refresh_dossier(mindset_id: str):
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    m.dossier = build_dossier(m.theme)
    m.dossier_ts = now_iso()
    store.save_mindset(m)
    return m.model_dump()


@app.get("/mindset/{mindset_id}")
def get_mindset(mindset_id: str):
    m = get_store().get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    return m.model_dump()


@app.get("/mindset")
def list_mindsets(owner_uid: Optional[str] = None):
    return [m.model_dump() for m in get_store().list_mindsets(owner_uid=owner_uid)]


@app.get("/mindset/{mindset_id}/rubric_versions")
def list_versions(mindset_id: str):
    return [v.model_dump() for v in get_store().list_rubric_versions(mindset_id)]


@app.post("/mindset/{mindset_id}/reflect")
def reflect(mindset_id: str):
    m = reflect_rubric(mindset_id)
    return m.model_dump()


@app.post("/hunt")
def hunt(req: HuntReq):
    store = get_store()
    m = store.get_mindset(req.mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
    # Pre-create the Hunt record so we can return an id immediately and the
    # client can poll while the background thread updates the trace.
    h = Hunt(mindset_id=req.mindset_id)
    h.status = "queued"
    h.trace.append({"t": now_iso(), "step": "queued"})
    store.save_hunt(h)

    def _run():
        try:
            run_hunt(req.mindset_id, budget=req.budget, hunt_id=h.id)
        except Exception as exc:
            logging.exception(f"background hunt {h.id} failed: {exc}")

    threading.Thread(target=_run, daemon=True).start()
    return {"hunt_id": h.id, "mindset_id": req.mindset_id, "status": h.status}


@app.get("/hunts/{mindset_id}")
def list_hunts(mindset_id: str, limit: int = 20):
    return [h.model_dump() for h in get_store().list_hunts(mindset_id, limit=limit)]


@app.get("/hunt/{mindset_id}/{hunt_id}")
def get_hunt(mindset_id: str, hunt_id: str):
    # MemoryStore can find by id alone; Firestore needs mindset context — both routes supported by passing mindset_id.
    store = get_store()
    try:
        h = store.get_hunt(hunt_id)
    except NotImplementedError:
        h = next((x for x in store.list_hunts(mindset_id, limit=200) if x.id == hunt_id), None)
    if not h:
        raise HTTPException(404, "hunt not found")
    return h.model_dump()


@app.get("/collection/{mindset_id}")
def collection(mindset_id: str, status: Optional[str] = None, min_score: float = 7.0, limit: int = 200):
    cs = get_store().list_candidates(mindset_id, status=status, limit=limit)
    if status is None:
        # default UI feed: liked always stays; otherwise surfaced + above threshold.
        cs = [c for c in cs if c.status == "liked" or (c.status == "surfaced" and (c.judge_score or 0) >= min_score)]
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
def feedback(req: FeedbackReq):
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
    return _publish_to_xdm_server(mindset_id, req.board_name, req.max_images, user_token, installation_id=req.installation_id)


def _publish_to_xdm_server(mindset_id: str, board_name: Optional[str], max_images: int, user_token: str, installation_id: Optional[str] = None):
    if not XDM_SERVER_URL:
        raise HTTPException(501, "XDM_SERVER_URL not configured")
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise HTTPException(404, "mindset not found")
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
    r = httpx.post(
        f"{XDM_SERVER_URL.rstrip('/')}/board_from_external_agent",
        json=payload,
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=30.0,
    )
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
