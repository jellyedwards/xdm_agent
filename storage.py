import os
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Protocol
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "memory")
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "(default)")
MEMORY_PERSIST_PATH = os.getenv("MEMORY_PERSIST_PATH", os.path.join(os.path.dirname(__file__), ".local_store.json"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class Subgenre(BaseModel):
    name: str
    description: str = ""


class Competition(BaseModel):
    name: str
    url: str = ""
    description: str = ""


class Gallery(BaseModel):
    name: str
    url: str = ""


class Creator(BaseModel):
    name: str
    why_notable: str = ""


class ThemeDossier(BaseModel):
    summary: str = ""
    visual_markers: List[str] = Field(default_factory=list)
    subgenres: List[Subgenre] = Field(default_factory=list)
    competitions: List[Competition] = Field(default_factory=list)
    galleries_and_archives: List[Gallery] = Field(default_factory=list)
    canonical_creators: List[Creator] = Field(default_factory=list)
    adjacent_themes: List[str] = Field(default_factory=list)
    suggested_queries: List[str] = Field(default_factory=list)
    cultural_context: str = ""


class Mindset(BaseModel):
    id: str = Field(default_factory=new_id)
    owner_uid: Optional[str] = None
    name: str
    theme: str
    rubric_text: str = ""
    dossier: Optional[ThemeDossier] = None
    dossier_ts: Optional[str] = None
    few_shot_image_ids: List[str] = Field(default_factory=list)
    tactic_prefs: Dict[str, float] = Field(default_factory=dict)
    serendipity: float = 0.2
    schedule: Dict[str, Any] = Field(default_factory=lambda: {"cron": None, "last_run": None, "next_run": None, "budget": 100})
    version: int = 1
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class RubricVersion(BaseModel):
    mindset_id: str
    version: int
    rubric_text: str
    reason_for_change: str = ""
    diff: str = ""
    ts: str = Field(default_factory=now_iso)


class Candidate(BaseModel):
    id: str = Field(default_factory=new_id)
    mindset_id: str
    hunt_id: Optional[str] = None
    image_url: str
    thumbnail_url: Optional[str] = None
    source_page_url: Optional[str] = None
    source_id: str
    discovered_via: Dict[str, Any] = Field(default_factory=dict)  # {agent, tactic, query, why}
    title: Optional[str] = None
    caption: Optional[str] = None
    creator: Optional[str] = None
    creator_url: Optional[str] = None
    year: Optional[int] = None
    license_name: Optional[str] = None
    license_url: Optional[str] = None
    attribution: Optional[str] = None
    rights_status: str = "unknown"  # clear|caveat|unknown
    phash: Optional[str] = None
    embedding_id: Optional[str] = None
    judge_score: Optional[float] = None
    judge_reason: Optional[str] = None
    judge_tags: List[str] = Field(default_factory=list)
    rubric_version_at_judge: Optional[int] = None
    status: str = "surfaced"  # surfaced|hidden|liked|disliked|dropped
    feedback_note: Optional[str] = None
    feedback_ts: Optional[str] = None
    found_at: str = Field(default_factory=now_iso)


class FeedbackEvent(BaseModel):
    id: str = Field(default_factory=new_id)
    mindset_id: str
    kind: str  # like|dislike|hide|direction
    image_id: Optional[str] = None
    direction_text: Optional[str] = None
    note: Optional[str] = None
    ts: str = Field(default_factory=now_iso)


class Hunt(BaseModel):
    id: str = Field(default_factory=new_id)
    mindset_id: str
    plan: List[Dict[str, Any]] = Field(default_factory=list)
    started_at: str = Field(default_factory=now_iso)
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    n_candidates: int = 0
    n_unique: int = 0
    n_rights_cleared: int = 0
    n_kept: int = 0
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    status: str = "running"  # running|completed|failed
    error: Optional[str] = None


class Store(Protocol):
    def save_mindset(self, m: Mindset) -> Mindset: ...
    def get_mindset(self, mindset_id: str) -> Optional[Mindset]: ...
    def list_mindsets(self, owner_uid: Optional[str] = None) -> List[Mindset]: ...
    def save_rubric_version(self, v: RubricVersion) -> None: ...
    def list_rubric_versions(self, mindset_id: str) -> List[RubricVersion]: ...
    def save_candidate(self, c: Candidate) -> Candidate: ...
    def update_candidate(self, c: Candidate) -> Candidate: ...
    def get_candidate(self, candidate_id: str) -> Optional[Candidate]: ...
    def list_candidates(self, mindset_id: str, status: Optional[str] = None, limit: int = 200) -> List[Candidate]: ...
    def candidate_exists_for_url(self, mindset_id: str, image_url: str) -> bool: ...
    def save_feedback(self, e: FeedbackEvent) -> None: ...
    def list_feedback(self, mindset_id: str, limit: int = 50) -> List[FeedbackEvent]: ...
    def save_hunt(self, h: Hunt) -> Hunt: ...
    def update_hunt(self, h: Hunt) -> Hunt: ...
    def get_hunt(self, hunt_id: str) -> Optional[Hunt]: ...
    def list_hunts(self, mindset_id: str, limit: int = 20) -> List[Hunt]: ...


class MemoryStore:
    def __init__(self, persist_path: Optional[str] = MEMORY_PERSIST_PATH):
        self.mindsets: Dict[str, Mindset] = {}
        self.rubric_versions: Dict[str, List[RubricVersion]] = {}
        self.candidates: Dict[str, Candidate] = {}
        self.url_index: Dict[str, str] = {}  # f"{mindset_id}\x1f{image_url}" -> candidate_id
        self.feedback: Dict[str, List[FeedbackEvent]] = {}
        self.hunts: Dict[str, Hunt] = {}
        self.persist_path = persist_path
        self._load()

    def _load(self):
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path) as f:
                d = json.load(f)
            self.mindsets = {k: Mindset(**v) for k, v in (d.get("mindsets") or {}).items()}
            self.rubric_versions = {k: [RubricVersion(**x) for x in v] for k, v in (d.get("rubric_versions") or {}).items()}
            self.candidates = {k: Candidate(**v) for k, v in (d.get("candidates") or {}).items()}
            self.feedback = {k: [FeedbackEvent(**x) for x in v] for k, v in (d.get("feedback") or {}).items()}
            self.hunts = {k: Hunt(**v) for k, v in (d.get("hunts") or {}).items()}
            for c in self.candidates.values():
                self.url_index[f"{c.mindset_id}\x1f{c.image_url}"] = c.id
            logging.info(f"MemoryStore loaded from {self.persist_path}: {len(self.mindsets)} mindsets, {len(self.candidates)} candidates")
        except Exception as exc:
            logging.warning(f"MemoryStore failed to load {self.persist_path}: {exc}")

    def _persist(self):
        if not self.persist_path:
            return
        try:
            data = {
                "mindsets": {k: v.model_dump() for k, v in self.mindsets.items()},
                "rubric_versions": {k: [x.model_dump() for x in v] for k, v in self.rubric_versions.items()},
                "candidates": {k: v.model_dump() for k, v in self.candidates.items()},
                "feedback": {k: [x.model_dump() for x in v] for k, v in self.feedback.items()},
                "hunts": {k: v.model_dump() for k, v in self.hunts.items()},
            }
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.persist_path)
        except Exception as exc:
            logging.warning(f"MemoryStore persist failed: {exc}")

    def save_mindset(self, m):
        m.updated_at = now_iso()
        self.mindsets[m.id] = m
        self._persist()
        return m

    def get_mindset(self, mindset_id):
        return self.mindsets.get(mindset_id)

    def list_mindsets(self, owner_uid=None):
        out = list(self.mindsets.values())
        if owner_uid is not None:
            out = [m for m in out if m.owner_uid == owner_uid]
        return sorted(out, key=lambda x: x.updated_at, reverse=True)

    def save_rubric_version(self, v):
        self.rubric_versions.setdefault(v.mindset_id, []).append(v)
        self._persist()

    def list_rubric_versions(self, mindset_id):
        return sorted(self.rubric_versions.get(mindset_id, []), key=lambda v: v.version)

    def save_candidate(self, c):
        self.candidates[c.id] = c
        self.url_index[f"{c.mindset_id}\x1f{c.image_url}"] = c.id
        self._persist()
        return c

    def update_candidate(self, c):
        self.candidates[c.id] = c
        self._persist()
        return c

    def get_candidate(self, candidate_id):
        return self.candidates.get(candidate_id)

    def list_candidates(self, mindset_id, status=None, limit=200):
        out = [c for c in self.candidates.values() if c.mindset_id == mindset_id]
        if status:
            out = [c for c in out if c.status == status]
        # Bucket by the candidate's hunt's started_at so all candidates in the
        # same hunt share the cohort key — then liked-first + score actually
        # take precedence inside the cohort.
        hunt_starts = {h.id: h.started_at for h in self.hunts.values() if h.mindset_id == mindset_id}
        def k(c):
            cohort = hunt_starts.get(c.hunt_id, c.found_at or "")
            return (cohort, c.status == "liked", c.judge_score or 0)
        out.sort(key=k, reverse=True)
        return out[:limit]

    def candidate_exists_for_url(self, mindset_id, image_url):
        return f"{mindset_id}\x1f{image_url}" in self.url_index

    def save_feedback(self, e):
        self.feedback.setdefault(e.mindset_id, []).append(e)
        self._persist()

    def list_feedback(self, mindset_id, limit=50):
        items = sorted(self.feedback.get(mindset_id, []), key=lambda x: x.ts, reverse=True)
        return items[:limit]

    def save_hunt(self, h):
        self.hunts[h.id] = h
        self._persist()
        return h

    def update_hunt(self, h):
        self.hunts[h.id] = h
        self._persist()
        return h

    def get_hunt(self, hunt_id):
        return self.hunts.get(hunt_id)

    def list_hunts(self, mindset_id, limit=20):
        out = [h for h in self.hunts.values() if h.mindset_id == mindset_id]
        out.sort(key=lambda h: h.started_at, reverse=True)
        return out[:limit]


class FirestoreStore:
    def __init__(self):
        from google.cloud import firestore
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        self.db = firestore.Client(project=project, database=FIRESTORE_DATABASE)
        self.mindsets = self.db.collection("mindsets")

    def _mindset_doc(self, mindset_id):
        return self.mindsets.document(mindset_id)

    def save_mindset(self, m):
        m.updated_at = now_iso()
        self._mindset_doc(m.id).set(m.model_dump())
        return m

    def get_mindset(self, mindset_id):
        snap = self._mindset_doc(mindset_id).get()
        if not snap.exists:
            return None
        return Mindset(**snap.to_dict())

    def list_mindsets(self, owner_uid=None):
        q = self.mindsets
        if owner_uid is not None:
            q = q.where("owner_uid", "==", owner_uid)
        return [Mindset(**d.to_dict()) for d in q.order_by("updated_at", direction="DESCENDING").stream()]

    def save_rubric_version(self, v):
        self._mindset_doc(v.mindset_id).collection("versions").document(str(v.version)).set(v.model_dump())

    def list_rubric_versions(self, mindset_id):
        docs = self._mindset_doc(mindset_id).collection("versions").order_by("version").stream()
        return [RubricVersion(**d.to_dict()) for d in docs]

    def save_candidate(self, c):
        self._mindset_doc(c.mindset_id).collection("candidates").document(c.id).set(c.model_dump())
        # url index doc for fast existence check
        from hashlib import sha1
        h = sha1(c.image_url.encode()).hexdigest()
        self._mindset_doc(c.mindset_id).collection("url_index").document(h).set({"candidate_id": c.id, "image_url": c.image_url})
        return c

    def update_candidate(self, c):
        self._mindset_doc(c.mindset_id).collection("candidates").document(c.id).set(c.model_dump())
        return c

    def get_candidate(self, candidate_id):
        # not indexed by id alone; expect callers to know the mindset. expensive scan fallback skipped.
        raise NotImplementedError("Firestore get_candidate requires mindset_id context")

    def list_candidates(self, mindset_id, status=None, limit=200):
        q = self._mindset_doc(mindset_id).collection("candidates")
        if status:
            q = q.where("status", "==", status)
        q = q.order_by("judge_score", direction="DESCENDING").limit(limit)
        return [Candidate(**d.to_dict()) for d in q.stream()]

    def candidate_exists_for_url(self, mindset_id, image_url):
        from hashlib import sha1
        h = sha1(image_url.encode()).hexdigest()
        return self._mindset_doc(mindset_id).collection("url_index").document(h).get().exists

    def save_feedback(self, e):
        self._mindset_doc(e.mindset_id).collection("feedback").document(e.id).set(e.model_dump())

    def list_feedback(self, mindset_id, limit=50):
        q = self._mindset_doc(mindset_id).collection("feedback").order_by("ts", direction="DESCENDING").limit(limit)
        return [FeedbackEvent(**d.to_dict()) for d in q.stream()]

    def save_hunt(self, h):
        self._mindset_doc(h.mindset_id).collection("hunts").document(h.id).set(h.model_dump())
        return h

    def update_hunt(self, h):
        self._mindset_doc(h.mindset_id).collection("hunts").document(h.id).set(h.model_dump())
        return h

    def get_hunt(self, hunt_id):
        raise NotImplementedError("Firestore get_hunt requires mindset_id context")

    def list_hunts(self, mindset_id, limit=20):
        q = self._mindset_doc(mindset_id).collection("hunts").order_by("started_at", direction="DESCENDING").limit(limit)
        return [Hunt(**d.to_dict()) for d in q.stream()]


_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is not None:
        return _store
    if STORAGE_BACKEND == "firestore":
        _store = FirestoreStore()
    else:
        _store = MemoryStore()
    logging.info(f"storage backend: {STORAGE_BACKEND}")
    return _store
