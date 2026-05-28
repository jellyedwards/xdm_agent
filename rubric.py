import os
import json
import logging
import difflib
from typing import List, Dict, Any, Optional
from google import genai
from google.genai import types
from dotenv import load_dotenv

from storage import Mindset, RubricVersion, FeedbackEvent, Candidate, ThemeDossier, get_store, now_iso

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_MODEL_LITE = os.getenv("GEMINI_MODEL_LITE", "gemini-flash-lite-latest")


TACTICS = [
    "prizewinner_archive",
    "feature_term_query",
    "adjacent_theme",
    "colour_family_query",
    "vintage_archive",
    "negative_space_probe",
    "creator_pursuit",
    "temporal_recency",
]

LIKE_BONUS = 0.10
DISLIKE_PENALTY = 0.05
TACTIC_FLOOR = 0.2
TACTIC_CEIL = 2.0


_gemini_client = None


def gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true"
    if use_vertex:
        _gemini_client = genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    else:
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise Exception("set GOOGLE_API_KEY or GEMINI_API_KEY in .env, or enable Vertex")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def default_tactic_prefs() -> Dict[str, float]:
    return {t: 1.0 for t in TACTICS}


SEED_PROMPT = """\
You are seeding a curation rubric for a designer's mood-board agent.

Theme: {theme}

{dossier_brief}

Produce a rubric in EXACTLY this format (no extra prose around it):

THEME: {theme}

POSITIVE — score higher for:
- <bullet>
- <bullet>
- <bullet>
- <bullet>

NEGATIVE — score lower for:
- <bullet>
- <bullet>
- <bullet>
- <bullet>

NOTES:

EXAMPLES:

Rules:
- Theme line is exactly "{theme}".
- 4-7 positive bullets emphasising abstract beauty, unusual subjects, striking composition, technique-specific aesthetic markers relevant to this theme. Use the dossier's visual markers if helpful.
- 4-6 negative bullets covering stock-photo clichés, textbook diagrams, watermarks, low resolution, snapshot quality, generic-corporate.
- Leave NOTES and EXAMPLES sections empty (just the header).
- Output nothing else.
"""


REFLECT_PROMPT = """\
You are refining a designer's curation rubric based on recent user feedback.

CURRENT RUBRIC:
{rubric}

RECENT FEEDBACK (newest first):
{feedback}

TASK:
Rewrite the rubric so future judging better reflects what the user likes. Rules:
- Preserve the THEME line verbatim.
- Keep total length under 280 words.
- Update NOTES with newly-emerging preferences in plain English, 2-5 bullets max.
- Refresh EXAMPLES with up to 4 LIKED and 4 DISLIKED entries from the feedback, newest first, each on its own line as "LIKED: <url> — <short reason>" or "DISLIKED: <url> — <short reason>".
- If the user's notes indicate a direction shift, broaden or pivot the POSITIVE/NEGATIVE bullets accordingly — do not just stack on top.
- Do not narrow the rubric beyond what the feedback actually says.

Output the new rubric only, no preamble.
"""


def _dossier_brief(d: Optional[ThemeDossier]) -> str:
    if not d:
        return ""
    lines = ["KNOWN ABOUT THIS THEME (use for context, don't repeat verbatim):"]
    if d.summary:
        lines.append(f"Summary: {d.summary}")
    if d.visual_markers:
        lines.append("Visual hallmarks: " + ", ".join(d.visual_markers[:8]))
    if d.subgenres:
        lines.append("Subgenres: " + ", ".join(s.name for s in d.subgenres[:6]))
    return "\n".join(lines)


def seed_rubric(theme: str, dossier: Optional[ThemeDossier] = None) -> str:
    prompt = SEED_PROMPT.format(theme=theme, dossier_brief=_dossier_brief(dossier))
    resp = gemini_client().models.generate_content(
        model=GEMINI_MODEL_LITE,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.4, max_output_tokens=700),
    )
    txt = (resp.text or "").strip()
    if not txt or "THEME:" not in txt:
        # last-resort deterministic fallback so tests/dev never explode
        txt = (
            f"THEME: {theme}\n\n"
            "POSITIVE — score higher for:\n"
            "- abstract beauty, unusual perspective, striking colour or form\n"
            "- compositions a designer would pin to a mood board\n"
            "- texture, scale, light handled with intent\n"
            "- novelty: something I haven't seen 100 times before\n\n"
            "NEGATIVE — score lower for:\n"
            "- generic stock-photo clichés\n"
            "- textbook diagrams, infographics, watermarks\n"
            "- low resolution or snapshot quality\n"
            "- corporate-promotional flavour\n\n"
            "NOTES:\n\n"
            "EXAMPLES:\n"
        )
    return txt


def initialise_mindset_rubric(m: Mindset) -> Mindset:
    m.rubric_text = seed_rubric(m.theme, dossier=m.dossier)
    m.tactic_prefs = m.tactic_prefs or default_tactic_prefs()
    m.version = 1
    store = get_store()
    store.save_mindset(m)
    store.save_rubric_version(RubricVersion(
        mindset_id=m.id, version=1, rubric_text=m.rubric_text,
        reason_for_change="seed", diff=m.rubric_text,
    ))
    return m


def feedback_since_last_reflect(mindset_id: str) -> List[FeedbackEvent]:
    store = get_store()
    versions = store.list_rubric_versions(mindset_id)
    if not versions:
        return []
    last_ts = versions[-1].ts
    return [e for e in store.list_feedback(mindset_id, limit=200) if e.ts > last_ts]


def reflect_if_pending(mindset_id: str) -> Optional[Mindset]:
    pending = feedback_since_last_reflect(mindset_id)
    signal = [e for e in pending if e.kind in ("like", "dislike", "direction")]
    if not signal:
        return None
    return reflect_rubric(mindset_id)


def _format_feedback_for_reflect(events: List[FeedbackEvent], cand_lookup: Dict[str, Candidate]) -> str:
    lines = []
    for e in events:
        c = cand_lookup.get(e.image_id or "")
        url = c.image_url if c else ""
        reason = (c.judge_reason or "")[:140] if c else ""
        if e.kind == "like":
            lines.append(f"- LIKED at {e.ts}: {url} — judge said \"{reason}\" — note: {e.note or ''}")
        elif e.kind == "dislike":
            lines.append(f"- DISLIKED at {e.ts}: {url} — judge said \"{reason}\" — note: {e.note or ''}")
        elif e.kind == "hide":
            lines.append(f"- HIDDEN at {e.ts}: {url}")
        elif e.kind == "direction":
            lines.append(f"- DIRECTION at {e.ts}: \"{e.direction_text or ''}\"")
    return "\n".join(lines) or "(none)"


def reflect_rubric(mindset_id: str) -> Mindset:
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise Exception(f"mindset not found: {mindset_id}")

    events = store.list_feedback(mindset_id, limit=20)
    if not events:
        logging.info("reflect: no feedback, skipping")
        return m

    image_ids = {e.image_id for e in events if e.image_id}
    cand_lookup = {}
    for cid in image_ids:
        c = store.get_candidate(cid) if cid else None
        if c:
            cand_lookup[cid] = c

    fb = _format_feedback_for_reflect(events, cand_lookup)
    prompt = REFLECT_PROMPT.format(rubric=m.rubric_text, feedback=fb)
    resp = gemini_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.5, max_output_tokens=900),
    )
    new_rubric = (resp.text or "").strip()
    if not new_rubric or "THEME:" not in new_rubric:
        logging.info(f"reflect: gemini returned no usable rubric, keeping current. raw: {resp.text!r}")
        return m

    old = m.rubric_text
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new_rubric.splitlines(),
        fromfile="rubric.v" + str(m.version), tofile="rubric.v" + str(m.version + 1), lineterm="",
    ))
    m.version += 1
    m.rubric_text = new_rubric
    store.save_mindset(m)
    store.save_rubric_version(RubricVersion(
        mindset_id=m.id, version=m.version, rubric_text=new_rubric,
        reason_for_change=f"reflected on {len(events)} feedback events",
        diff=diff,
    ))
    return m


def update_tactic_prefs(mindset_id: str, event: FeedbackEvent) -> None:
    if event.kind not in ("like", "dislike"):
        return
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m or not event.image_id:
        return
    c = store.get_candidate(event.image_id)
    if not c:
        return
    tactic = (c.discovered_via or {}).get("tactic")
    if not tactic:
        return
    cur = m.tactic_prefs.get(tactic, 1.0)
    delta = LIKE_BONUS if event.kind == "like" else -DISLIKE_PENALTY
    m.tactic_prefs[tactic] = max(TACTIC_FLOOR, min(TACTIC_CEIL, cur + delta))
    store.save_mindset(m)


def record_feedback(mindset_id: str, kind: str, image_id: str = None, direction_text: str = None, note: str = None, reflect_every: int = 5) -> Dict[str, Any]:
    store = get_store()
    e = FeedbackEvent(mindset_id=mindset_id, kind=kind, image_id=image_id, direction_text=direction_text, note=note)
    store.save_feedback(e)

    if image_id and kind in ("like", "dislike", "hide"):
        c = store.get_candidate(image_id)
        if c:
            c.status = {"like": "liked", "dislike": "disliked", "hide": "surfaced"}[kind]
            c.feedback_note = note
            c.feedback_ts = e.ts
            store.update_candidate(c)
        if kind in ("like", "dislike"):
            update_tactic_prefs(mindset_id, e)

    reflected = False
    if kind == "direction":
        reflect_rubric(mindset_id)
        reflected = True
    else:
        recent = store.list_feedback(mindset_id, limit=reflect_every)
        if len(recent) >= reflect_every and all(r.kind != "direction" for r in recent):
            # only reflect when we just hit a clean multiple of reflect_every
            total = len(store.list_feedback(mindset_id, limit=10_000))
            if total % reflect_every == 0:
                reflect_rubric(mindset_id)
                reflected = True

    return {"feedback_id": e.id, "reflected": reflected}
