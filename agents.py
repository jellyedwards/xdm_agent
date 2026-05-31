import os
import json
import logging
import time
import asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from dotenv import load_dotenv

from storage import Mindset, Candidate, Hunt, ThemeDossier, get_store, now_iso, new_id
from sources import SOURCE_REGISTRY, SEARCH_FUNCS, run_source
from dedup import canonicalise_url, url_fingerprint, phash_url, is_near_duplicate
from rubric import gemini_client, GEMINI_MODEL, GEMINI_MODEL_LITE, TACTICS, default_tactic_prefs, initialise_mindset_rubric, reflect_if_pending

load_dotenv()

JUDGE_CONCURRENCY = int(os.getenv("JUDGE_CONCURRENCY", "6"))
SOURCE_CONCURRENCY = int(os.getenv("SOURCE_CONCURRENCY", "8"))
PHASH_CONCURRENCY = int(os.getenv("PHASH_CONCURRENCY", "8"))
DEFAULT_HUNT_BUDGET = int(os.getenv("DEFAULT_HUNT_BUDGET", "20"))
DEFAULT_PER_SOURCE = int(os.getenv("DEFAULT_PER_SOURCE", "10"))
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "7.0"))
PHASH_NEAR_DUP_THRESHOLD = int(os.getenv("PHASH_NEAR_DUP_THRESHOLD", "10"))
CURATE_DIVERSITY_THRESHOLD = int(os.getenv("CURATE_DIVERSITY_THRESHOLD", "4"))


class PlannedSearch(BaseModel):
    source_id: str
    query: str
    tactic: str
    why: str = ""


class HuntPlan(BaseModel):
    searches: List[PlannedSearch]
    reasoning: str = ""


class JudgeResult(BaseModel):
    description: str
    score: float = Field(ge=0, le=10)
    reason: str = Field(
        description="One or two sentences describing the image's concrete visual "
        "qualities (subject, composition, colour, light, technique). Shown to the "
        "user as a caption — never mention the rubric, scoring, or how well it "
        "matches the theme; just describe what the image shows."
    )
    tags: List[str] = Field(default_factory=list)


SCOUT_PROMPT = """\
You are the Scout in a multi-agent visual-curation system. Your job is to plan a hunt for the most striking, mood-board-worthy images on a theme.

THEME: {theme}

DOSSIER (your theme knowledge — competitions, subgenres, suggested queries, creators worth chasing):
{dossier_brief}

CURRENT RUBRIC (what the judge will reward):
{rubric}

LIKED IMAGES (positive signal — find more in this vein):
{liked_brief}

AVAILABLE SOURCES (id : kind : licensing posture):
{source_list}

TACTICS (your moves — bias toward higher-scored ones, but don't ignore the others):
{tactic_list}

RECENTLY-USED QUERIES (avoid repeating verbatim):
{recent_queries}

PRODUCE A HUNT PLAN. Return a JSON object matching this schema:
{{
  "reasoning": "<one paragraph on the angle you're taking this hunt>",
  "searches": [
    {{
      "source_id": "<one of the source ids>",
      "query": "<the exact query string to send to that source>",
      "tactic": "<one of the tactic names>",
      "why": "<one sentence on why this combination>"
    }}
  ]
}}

Rules:
- Produce {budget} searches.
- Use the dossier's suggested queries and known competitions/creators as seed material, but invent variations and new angles too — don't just copy them.
- Favour evocative, non-literal queries — not the theme word itself. Invent metaphors, technique names, material descriptors, adjacent concepts.
- If LIKED IMAGES are listed, dedicate at least 2 searches to finding more in their vein — try the creator_pursuit tactic on their creators, or queries that capture what made them strong.
- Use a mix of sources matching the tactic. Competition archives for prizewinner_archive. Museums for vintage_archive. Stock + web for feature_term_query. Web for adjacent_theme. Etc.
- Serendipity dial is {serendipity:.2f} — at high values, include 1-2 deliberately surprising probes (e.g. negative_space_probe).
- Each query should plausibly return images this rubric would score well.

Output JSON only.
"""


DOSSIER_PROMPT = """\
You are a visual researcher building a knowledge dossier for an autonomous mood-board curator.

THEME: {theme}

Build a deep dossier covering this theme as a visual genre:
- 2-3 sentence summary positioning it.
- 5-8 concrete visual hallmarks of great work in this space.
- Main subgenres or stylistic schools (name + one-line description).
- Real competitions / annual showcases that surface the best examples. Include URLs you're confident about; leave url empty rather than invent.
- Major galleries, archives, museums, or specialist sites worth watching.
- Canonical creators / pioneers (name + one-line why-notable).
- Adjacent themes that feed it inspiration.
- 10-14 specific evocative search queries that would surface striking work. NOT containing the theme word literally. Phrases, not single words. Map to real techniques, materials, subjects, or moods.
- Short paragraph on cultural / artistic significance.

Rules:
- Be specific. Name real things. Prefer concrete ("Nikon Small World") over vague ("microscopy competitions").
- Don't invent. Only list competitions / sites / creators you're confident actually exist.
- Where uncertain about a URL, leave it empty.

Return STRICT JSON matching the schema.
"""


JUDGE_PROMPT = """\
You are the Judge in a visual curation system. Score one image against the rubric.

RUBRIC:
{rubric}

Look at the image and return JSON matching:
{{
  "description": "<one short sentence describing what the image actually shows>",
  "score": <number 0-10, higher = a stronger match for the rubric>,
  "reason": "<one or two sentences describing the image's concrete visual qualities — subject, composition, colour, light, technique>",
  "tags": [<3-6 short descriptive tags>]
}}

The rubric only informs the SCORE. The "reason" is shown to the user as a
caption, so it must describe the image itself — never mention the rubric,
scoring, or how well it matches (no "this matches the rubric", "fits the
theme", "strong match", etc.). Just describe what you see.

Be honest. Score below 5 if the image is generic, low quality, or off-rubric.
Output JSON only.
"""


def _source_list_for_prompt() -> str:
    lines = []
    for sid, meta in SOURCE_REGISTRY.items():
        lines.append(f"  - {sid} : {meta['kind']} : {meta['license_default']}")
    return "\n".join(lines)


def _tactic_list_for_prompt(prefs: Dict[str, float]) -> str:
    out = []
    for t in TACTICS:
        out.append(f"  - {t} (pref {prefs.get(t, 1.0):.2f})")
    return "\n".join(out)


def _recent_queries_for_prompt(mindset_id: str, limit: int = 30) -> str:
    store = get_store()
    hunts = store.list_hunts(mindset_id, limit=5)
    qs = []
    for h in hunts:
        for s in h.plan or []:
            q = s.get("query") if isinstance(s, dict) else None
            if q:
                qs.append(q)
    qs = qs[:limit]
    return "\n".join(f"  - {q}" for q in qs) or "  (none yet)"


def _dossier_brief_for_scout(d) -> str:
    if not d:
        return "  (no dossier yet — work from theme alone)"
    out = []
    if d.summary:
        out.append(f"  Summary: {d.summary}")
    if d.visual_markers:
        out.append("  Visual hallmarks: " + ", ".join(d.visual_markers[:10]))
    if d.subgenres:
        out.append("  Subgenres: " + ", ".join(f"{s.name}" for s in d.subgenres[:8]))
    if d.competitions:
        out.append("  Competitions/showcases: " + ", ".join(c.name for c in d.competitions[:8]))
    if d.canonical_creators:
        out.append("  Canonical creators: " + ", ".join(c.name for c in d.canonical_creators[:10]))
    if d.adjacent_themes:
        out.append("  Adjacent themes: " + ", ".join(d.adjacent_themes[:8]))
    if d.suggested_queries:
        out.append("  Dossier-suggested queries:")
        for q in d.suggested_queries[:14]:
            out.append(f"    - {q}")
    return "\n".join(out)


def _liked_brief_for_scout(mindset_id: str, max_n: int = 6) -> str:
    store = get_store()
    liked = [c for c in store.list_candidates(mindset_id, status="liked", limit=50)]
    liked.sort(key=lambda c: c.feedback_ts or "", reverse=True)
    if not liked:
        return "  (no likes yet)"
    out = []
    for c in liked[:max_n]:
        out.append(f"  - [{c.source_id}] {(c.title or '').strip()[:60] or '(untitled)'} by {c.creator or 'unknown'} — judge said: \"{(c.judge_reason or '')[:140]}\"")
    return "\n".join(out)


def plan_hunt(mindset_id: str, budget: int = None) -> HuntPlan:
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise Exception(f"mindset not found: {mindset_id}")
    if not m.tactic_prefs:
        m.tactic_prefs = default_tactic_prefs()
    budget = budget or DEFAULT_HUNT_BUDGET

    prompt = SCOUT_PROMPT.format(
        theme=m.theme,
        dossier_brief=_dossier_brief_for_scout(m.dossier),
        rubric=m.rubric_text or "(rubric not seeded — invent a sensible one)",
        liked_brief=_liked_brief_for_scout(mindset_id),
        source_list=_source_list_for_prompt(),
        tactic_list=_tactic_list_for_prompt(m.tactic_prefs),
        recent_queries=_recent_queries_for_prompt(mindset_id),
        budget=budget,
        serendipity=m.serendipity,
    )
    raw = scout_complete(prompt)
    data = None
    try:
        data = json.loads(raw)
    except Exception as exc:
        logging.info(f"plan_hunt: strict parse failed ({exc}); salvaging on {len(raw)} chars")
        data = _salvage_json(raw)
    if data:
        try:
            plan = HuntPlan(**data)
        except Exception as exc:
            logging.info(f"plan_hunt: model build failed ({exc}); using fallback")
            data = None
    if not data:
        plan = HuntPlan(searches=[
            PlannedSearch(source_id="unsplash", query=m.theme, tactic="feature_term_query"),
            PlannedSearch(source_id="met", query=m.theme, tactic="vintage_archive"),
            PlannedSearch(source_id="nasa", query=m.theme, tactic="adjacent_theme"),
            PlannedSearch(source_id="wikimedia", query=m.theme, tactic="feature_term_query"),
            PlannedSearch(source_id="evident_ioty", query=m.theme, tactic="prizewinner_archive"),
        ], reasoning="fallback plan (LLM response was unparseable)")

    # filter to known sources, dedup identical (source, query) pairs
    seen = set()
    filtered = []
    for s in plan.searches:
        if s.source_id not in SEARCH_FUNCS:
            continue
        key = (s.source_id, s.query.lower())
        if key in seen:
            continue
        seen.add(key)
        filtered.append(s)
    plan.searches = filtered[: budget * 2]
    return plan


def execute_searches(mindset_id: str, plan: HuntPlan, per_source: int = None, on_source_done=None) -> List[Candidate]:
    per_source = per_source or DEFAULT_PER_SOURCE
    candidates: List[Candidate] = []
    with ThreadPoolExecutor(max_workers=SOURCE_CONCURRENCY) as pool:
        futures = {}
        for s in plan.searches:
            fut = pool.submit(run_source, s.source_id, mindset_id, s.query, per_source)
            futures[fut] = s
        done_n = 0
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                got = fut.result() or []
            except Exception as exc:
                logging.info(f"source {s.source_id} threw: {exc}")
                got = []
            for c in got:
                c.discovered_via = {"agent": "scout", "tactic": s.tactic, "source_id": s.source_id, "query": s.query, "why": s.why}
                candidates.append(c)
            done_n += 1
            logging.info(f"source {s.source_id} for {s.query!r}: {len(got)} candidates ({done_n}/{len(plan.searches)})")
            if on_source_done:
                try: on_source_done(s, got, done_n, len(plan.searches))
                except Exception as exc: logging.info(f"on_source_done hook failed: {exc}")
    return candidates


def dedupe_candidates(mindset_id: str, candidates: List[Candidate]) -> List[Candidate]:
    store = get_store()
    seen_urls = set()
    out: List[Candidate] = []
    # url-canonical dedup intra-batch
    for c in candidates:
        cu = canonicalise_url(c.image_url)
        if cu in seen_urls:
            continue
        seen_urls.add(cu)
        c.image_url = cu
        out.append(c)
    # drop anything already in the mindset's collection
    out = [c for c in out if not store.candidate_exists_for_url(mindset_id, c.image_url)]
    # phash near-dup intra-batch — parallel fetch
    with ThreadPoolExecutor(max_workers=PHASH_CONCURRENCY) as pool:
        futures = {pool.submit(phash_url, c.thumbnail_url or c.image_url): c for c in out}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                c.phash = fut.result() or ""
            except Exception as exc:
                logging.info(f"phash failed for {c.image_url}: {exc}")
                c.phash = ""
    kept: List[Candidate] = []
    for c in out:
        dup = False
        if c.phash:
            for k in kept:
                if k.phash and is_near_duplicate(c.phash, k.phash, PHASH_NEAR_DUP_THRESHOLD):
                    dup = True
                    break
        if not dup:
            kept.append(c)
    return kept


def apply_rights(mindset_id: str, candidates: List[Candidate], allow_unknown: bool = False) -> List[Candidate]:
    out = []
    for c in candidates:
        if c.rights_status == "unknown" and not allow_unknown:
            continue
        out.append(c)
    return out


def judge_one(mindset: Mindset, c: Candidate) -> Candidate:
    rubric = mindset.rubric_text or f"THEME: {mindset.theme}\n(use general design taste)"
    raw = judge_complete(rubric, c.image_url)
    try:
        jr = JudgeResult(**json.loads(raw))
    except Exception as exc:
        logging.info(f"judge parse failed for {c.image_url}: {exc}; raw: {raw!r}")
        jr = JudgeResult(description="", score=0.0, reason="judge failed to parse", tags=[])
    c.judge_score = jr.score
    c.judge_reason = jr.reason
    c.judge_tags = jr.tags
    c.caption = c.caption or jr.description
    c.rubric_version_at_judge = mindset.version
    return c


def judge_batch(mindset: Mindset, candidates: List[Candidate], on_progress=None) -> List[Candidate]:
    out: List[Candidate] = []
    total = len(candidates)
    with ThreadPoolExecutor(max_workers=JUDGE_CONCURRENCY) as pool:
        futures = {pool.submit(judge_one, mindset, c): c for c in candidates}
        for fut in as_completed(futures):
            try:
                out.append(fut.result())
            except Exception as exc:
                c = futures[fut]
                logging.info(f"judge failed for {c.image_url}: {exc}")
                c.judge_score = 0.0
                c.judge_reason = f"judge error: {exc}"
                out.append(c)
            if on_progress:
                try: on_progress(len(out), total)
                except Exception as exc: logging.info(f"judge on_progress hook failed: {exc}")
    return out


def curate(candidates: List[Candidate], threshold: float = SCORE_THRESHOLD, max_keep: int = 20) -> List[Candidate]:
    eligible = [c for c in candidates if (c.judge_score or 0) >= threshold]
    eligible.sort(key=lambda c: c.judge_score or 0, reverse=True)
    kept: List[Candidate] = []
    for c in eligible:
        if len(kept) >= max_keep:
            break
        dup = False
        if c.phash:
            for k in kept:
                if k.phash and is_near_duplicate(c.phash, k.phash, CURATE_DIVERSITY_THRESHOLD):
                    dup = True
                    break
        # belt-and-braces: also drop if same source + same creator + same title prefix
        if not dup:
            ck = (c.source_id, (c.creator or "").lower(), (c.title or "").strip().lower()[:32])
            for k in kept:
                kk = (k.source_id, (k.creator or "").lower(), (k.title or "").strip().lower()[:32])
                if ck == kk and ck[2]:
                    dup = True
                    break
        if dup:
            continue
        c.status = "surfaced"
        kept.append(c)
    kept_ids = {c.id for c in kept}
    for c in candidates:
        if c.id not in kept_ids:
            c.status = "dropped"
    return kept


def _salvage_json(text: str):
    """Best-effort recovery of a JSON object whose trailing tokens were chopped.
    Walks the string, tracks bracket/quote state, then closes anything still open."""
    if not text:
        return None
    s = text.strip()
    # try a clean parse first
    try:
        return json.loads(s)
    except Exception:
        pass
    # heuristic close: cut at the last comma/colon and seal open structures
    stack = []
    in_str = False
    esc = False
    last_safe = 0
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe = i + 1
        elif ch == ",":
            last_safe = i  # safe to cut before this comma
    head = s[:last_safe] if last_safe else s
    if in_str:
        head = head + '"'
    # close any still-open structures in reverse order
    for opener in reversed(stack):
        head += "}" if opener == "{" else "]"
    try:
        return json.loads(head)
    except Exception as exc:
        logging.info(f"_salvage_json gave up: {exc}")
        return None


def build_dossier(theme: str) -> ThemeDossier:
    logging.info(f"build_dossier: {theme=}")
    resp = gemini_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=DOSSIER_PROMPT.format(theme=theme),
        config=types.GenerateContentConfig(
            temperature=0.5, max_output_tokens=8000,
            response_mime_type="application/json",
            response_schema=ThemeDossier,
        ),
    )
    raw = resp.text or ""
    data = None
    try:
        data = json.loads(raw)
    except Exception as exc:
        logging.warning(f"dossier strict-parse failed ({exc}); attempting salvage on {len(raw)} chars")
        data = _salvage_json(raw)
    if not data:
        return ThemeDossier(summary=f"Visual research on {theme}.")
    try:
        d = ThemeDossier(**data)
        logging.info(f"dossier: {len(d.competitions)} competitions, {len(d.canonical_creators)} creators, {len(d.suggested_queries)} queries")
        return d
    except Exception as exc:
        logging.warning(f"dossier model build failed: {exc}; data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        return ThemeDossier(summary=f"Visual research on {theme}.")


def initialise_mindset_full(m: Mindset) -> Mindset:
    """Build dossier first (so rubric seed is informed), then seed rubric."""
    store = get_store()
    if not m.dossier:
        try:
            m.dossier = build_dossier(m.theme)
            m.dossier_ts = now_iso()
            store.save_mindset(m)
        except Exception as exc:
            logging.warning(f"dossier build failed: {exc}")
    if not m.rubric_text:
        try:
            m = initialise_mindset_rubric(m)
        except Exception as exc:
            logging.warning(f"rubric seed failed: {exc}")
    return m


def run_hunt(mindset_id: str, budget: int = None, hunt_id: str = None) -> Hunt:
    store = get_store()
    m = store.get_mindset(mindset_id)
    if not m:
        raise Exception(f"mindset not found: {mindset_id}")

    # Cold-start safety: if the mindset was created before dossier/rubric were a thing,
    # or the seed failed (no key), fix it up now.
    if not m.dossier or not m.rubric_text:
        m = initialise_mindset_full(m)

    # Use the pre-created hunt record if the caller spawned one (async path),
    # otherwise create a fresh one (sync path).
    h = None
    if hunt_id:
        h = store.get_hunt(hunt_id, mindset_id)
    if not h:
        h = Hunt(id=hunt_id or new_id(), mindset_id=mindset_id)

    def _step(name: str, **extra):
        h.trace.append({"t": now_iso(), "step": name, **extra})
        store.update_hunt(h)
        logging.info(f"hunt {h.id} step={name} {extra}")

    h.status = "running"
    _step("start", mindset_version=m.version)

    try:
        t0 = time.perf_counter()

        _step("reflecting")
        try:
            refreshed = reflect_if_pending(mindset_id)
            if refreshed:
                m = refreshed
                _step("reflected", new_version=m.version)
            else:
                _step("reflect_skipped", reason="no new signal")
        except Exception as exc:
            logging.warning(f"auto-reflect skipped: {exc}")
            _step("reflect_skipped", reason=str(exc))

        _step("planning")
        plan = plan_hunt(mindset_id, budget=budget)
        h.plan = [s.model_dump() for s in plan.searches]
        _step("planned", n_searches=len(plan.searches), reasoning=plan.reasoning)

        _step("searching", n_searches=len(plan.searches))
        def _on_source_done(s, got, done_n, total):
            h.trace.append({"t": now_iso(), "step": "source_done", "source_id": s.source_id, "query": s.query, "tactic": s.tactic, "found": len(got), "done": done_n, "of": total})
            store.update_hunt(h)
        raw = execute_searches(mindset_id, plan, on_source_done=_on_source_done)
        h.n_candidates = len(raw)
        _step("searched", n_raw=len(raw))

        _step("deduping", of=len(raw))
        unique = dedupe_candidates(mindset_id, raw)
        h.n_unique = len(unique)
        _step("deduped", n_unique=len(unique), removed=len(raw) - len(unique))

        _step("checking_rights", of=len(unique))
        cleared = apply_rights(mindset_id, unique, allow_unknown=False)
        h.n_rights_cleared = len(cleared)
        _step("rights_checked", n_cleared=len(cleared), n_dropped=len(unique) - len(cleared))

        _step("judging", of=len(cleared))
        def _on_judge_progress(done, total):
            # only push trace update every ~5 to avoid hammering disk
            if done == total or done % 5 == 0:
                h.trace.append({"t": now_iso(), "step": "judge_progress", "done": done, "of": total})
                store.update_hunt(h)
        judged = judge_batch(m, cleared, on_progress=_on_judge_progress)
        for c in judged:
            c.hunt_id = h.id
            store.save_candidate(c)
        avg_score = round(sum(c.judge_score or 0 for c in judged) / max(len(judged), 1), 2)
        above = sum(1 for c in judged if (c.judge_score or 0) >= SCORE_THRESHOLD)
        _step("judged", n_judged=len(judged), avg_score=avg_score, above_threshold=above)

        _step("curating")
        kept = curate(judged)
        for c in judged:
            store.update_candidate(c)
        h.n_kept = len(kept)
        _step("curated", n_kept=len(kept))

        h.status = "completed"
        h.completed_at = now_iso()
        h.duration_ms = int((time.perf_counter() - t0) * 1000)
        _step("done", n_kept=len(kept), duration_ms=h.duration_ms)
        logging.info(f"hunt {h.id} completed in {h.duration_ms}ms: kept {h.n_kept}/{h.n_candidates}")
    except Exception as exc:
        h.status = "failed"
        h.error = str(exc)
        h.completed_at = now_iso()
        _step("error", error=str(exc))
        logging.exception(f"hunt {h.id} failed: {exc}")
        raise

    return h


# ── ADK orchestration — the reasoning actually runs through a Runner ─────────
# Scout (hunt planning) and Judge (per-image scoring) are the two real LLM
# decision points in the pipeline. Both execute as ADK LlmAgents driven by an
# InMemoryRunner — this is what makes the system ADK-*orchestrated* rather than
# ADK-as-an-unused-dependency. The deterministic steps (parallel search, dedupe,
# rights, curate) stay as plain Python: ADK drives the reasoning, Python drives
# the plumbing. `run_hunt` remains the canonical entrypoint and sequences them.
#
# Set XDM_USE_ADK=0 to fall back to direct google-genai calls (also the
# automatic fallback if the ADK path raises, so a demo can never hard-fail).

try:
    from google.adk.agents import LlmAgent, SequentialAgent
    from google.adk.runners import InMemoryRunner
    _ADK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - ADK is a pinned dependency
    logging.warning(f"google.adk unavailable ({exc}); ADK orchestration disabled")
    _ADK_AVAILABLE = False

ADK_APP = "xdm_agent"
USE_ADK = _ADK_AVAILABLE and os.getenv("XDM_USE_ADK", "1").lower() in ("1", "true", "yes")

SCOUT_INSTRUCTION = (
    "You are the Scout in a multi-agent visual-curation system. You receive a "
    "fully-specified hunt brief and produce a hunt plan as JSON matching the "
    "required schema. Output JSON only."
)
JUDGE_INSTRUCTION = (
    "You are the Judge in a visual-curation system. Score the single supplied "
    "image against the supplied rubric and return JSON matching the required "
    "schema. Be honest; score low for generic, low-quality, or off-rubric work. "
    "The rubric only informs the score — the 'reason' is shown to the user as a "
    "caption, so describe the image's concrete visual qualities and never mention "
    "the rubric, scoring, or how well it matches the theme. Output JSON only."
)

scout_agent = None
judge_agent = None
root_agent = None


def _make_scout_agent():
    return LlmAgent(
        name="scout", model=GEMINI_MODEL, instruction=SCOUT_INSTRUCTION,
        output_schema=HuntPlan, output_key="hunt_plan",
        generate_content_config=types.GenerateContentConfig(temperature=0.85, max_output_tokens=4000),
    )


def _make_judge_agent():
    return LlmAgent(
        name="judge", model=GEMINI_MODEL, instruction=JUDGE_INSTRUCTION,
        output_schema=JudgeResult, output_key="judge_result",
        generate_content_config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=1400),
    )


def build_adk_agents():
    """Build the ADK agents once. `scout_agent`/`judge_agent` are parent-free so
    they can be driven directly by a runner; `root_agent` is a real top-level
    ADK SequentialAgent (its own child instances) — the discoverable workflow
    object for `adk web` and the 'ADK orchestration' requirement."""
    global scout_agent, judge_agent, root_agent
    if not _ADK_AVAILABLE:
        return None
    if root_agent is not None:
        return root_agent
    scout_agent = _make_scout_agent()
    judge_agent = _make_judge_agent()
    root_agent = SequentialAgent(name="xdm_curator", sub_agents=[_make_scout_agent(), _make_judge_agent()])
    return root_agent


def _adk_complete(agent, parts, user_id: str = "xdm") -> str:
    """Run an ADK LlmAgent to completion over a single user message and return the
    final response text. With output_schema set, that text is the JSON payload.

    Driven via asyncio.run on the agent's async APIs. Every caller (the hunt
    thread, the FastAPI threadpool worker for /a2a, the judge ThreadPoolExecutor
    workers) runs without a live event loop, so this is safe; if one ever isn't,
    asyncio.run raises and the callers below fall back to a direct genai call."""
    async def _go() -> str:
        runner = InMemoryRunner(agent=agent, app_name=ADK_APP)
        try:
            sess = await runner.session_service.create_session(app_name=ADK_APP, user_id=user_id)
            msg = types.Content(role="user", parts=parts)
            chunks: List[str] = []
            async for ev in runner.run_async(user_id=user_id, session_id=sess.id, new_message=msg):
                if ev.is_final_response() and ev.content and ev.content.parts:
                    chunks += [p.text for p in ev.content.parts if p.text]
            return "".join(chunks)
        finally:
            await runner.close()

    return asyncio.run(_go())


def scout_complete(prompt: str) -> str:
    """Scout reasoning. Routes through the ADK runner; falls back to a direct
    google-genai call if ADK is disabled or errors. Returns raw JSON text."""
    if USE_ADK:
        try:
            build_adk_agents()
            txt = _adk_complete(scout_agent, [types.Part.from_text(text=prompt)])
            if txt.strip():
                return txt
            logging.info("scout: ADK returned empty; falling back to direct genai")
        except Exception as exc:
            logging.warning(f"scout: ADK path failed ({exc}); falling back to direct genai")
    resp = gemini_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.85, max_output_tokens=4000,
            response_mime_type="application/json", response_schema=HuntPlan,
        ),
    )
    return resp.text or ""


def judge_complete(rubric: str, image_url: str) -> str:
    """Judge reasoning for one image. Routes through the ADK runner; falls back to
    a direct google-genai call if ADK is disabled or errors. Returns raw JSON text."""
    prompt = JUDGE_PROMPT.format(rubric=rubric)
    image_part = types.Part.from_uri(file_uri=image_url, mime_type="image/jpeg")
    if USE_ADK:
        try:
            build_adk_agents()
            txt = _adk_complete(judge_agent, [image_part, types.Part.from_text(text=prompt)])
            if txt.strip():
                return txt
            logging.info("judge: ADK returned empty; falling back to direct genai")
        except Exception as exc:
            logging.warning(f"judge: ADK path failed ({exc}); falling back to direct genai")
    resp = gemini_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=[image_part, prompt],
        config=types.GenerateContentConfig(
            temperature=0.3, max_output_tokens=1400,
            response_mime_type="application/json", response_schema=JudgeResult,
        ),
    )
    return resp.text or ""
