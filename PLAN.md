# xdm_agent — Build Plan

> Deadline: **2026-06-05** (8 days from 2026-05-28). Track 2 (Optimize).

## Concept refresh (one sentence)

A multi-agent, Gemini-powered visual curator that owns an evolving "mindset" per theme, hunts widely across the open web, judges every candidate against a rubric that learns from user feedback, and keeps doing it on a schedule — with full provenance and licence respect baked in.

## Why this is a genuine "agent", not a chatbot

Five tests this passes that a chat wrapper fails:

1. **Autonomy** — it acts without being prompted (Cloud Scheduler → hunt → produce new feed).
2. **Tool use** — 10+ external source tools, dedup tools, vision-judge tool, persistence tool, A2A endpoint.
3. **State** — persistent mindset, rubric history, feedback memory, "what I've already shown you" memory.
4. **Goal-directed planning** — Scout produces a hunt plan from goals + history, doesn't just answer the immediate prompt.
5. **Learning** — rubric and tactic preferences mutate from feedback, observably between versions.

## Layout style

Matching `xdm_server`'s style: a few long sibling files at top level, no nested packages, minimal comments, casual names. The agents below all live in `agents.py`; the source query functions all live in `sources.py`. See [ARCHITECTURE.md](./ARCHITECTURE.md#layout) for the file map.

## Multi-agent decomposition

Each component is an ADK Agent (or a Workflow node). All live in `agents.py`.

| Agent | Responsibility | Inputs | Outputs |
|---|---|---|---|
| **MindsetAgent** | Owns the mindset record. Loads/persists rubric, tactics, history. Not really an LLM agent — more a state controller wrapped as one for observability. | mindset_id | Mindset (rubric, tactics, few-shot, schedule) |
| **ScoutAgent** | Decides *which* sources and *what* queries for this hunt. Considers prior hunts, seasonality, tactic performance. | Mindset, prior hunt history | `HuntPlan` (list of `{source_id, query, why}`) |
| **SearchExecutor** | Fans out the plan to source tools in parallel. ADK ParallelAgent / Workflow parallel node. | HuntPlan | Raw candidates |
| **DedupeAgent** | URL canonicalise → perceptual hash → embedding similarity. Drops near-duplicates and anything already in the mindset's collection. | Raw candidates, existing collection | Unique candidates |
| **RightsAgent** | Looks up source's licence policy. Attempts page-level licence detection for ambiguous sources. Drops anything `rights_status=unknown` unless user opts in. | Unique candidates | Rights-cleared candidates with attribution strings |
| **JudgeAgent** | Gemini multimodal call per image; scores 0–10 vs current rubric; returns reasoning + tags. | Rights-cleared candidates, Mindset.rubric | Judged candidates |
| **CuratorAgent** | Threshold + diversity penalty + ranking. Persists kept set. Emits digest. | Judged candidates | Persisted collection + digest |
| **FeedbackAgent** | Reads recent likes/dislikes/direction-text. Calls Gemini reflection to rewrite rubric. Versions mindset. Updates tactic-preference bandit. | New feedback events | New Mindset version |
| **SchedulerAgent** | Per-mindset cron config. Triggers HuntWorkflow. Adjusts hunt budget based on prior yield. | Mindset.schedule | HuntWorkflow invocation |
| **PublisherAgent** *(A2A)* | Exposes the curator over A2A. Lets external agents request a curated set for a brief. | Theme + brief (A2A message) | Collection JSON |

The **HuntWorkflow** wires Scout → SearchExecutor → Dedupe → Rights → Judge → Curator as an ADK Workflow. **FeedbackAgent** and **SchedulerAgent** are separate, triggered by user action and Cloud Scheduler respectively.

## Data model (Firestore)

```
mindsets/{mindset_id}                            # current state
  ├── name, theme, owner_uid
  ├── rubric_text                                # current rubric
  ├── few_shot_image_ids[]                       # positive/negative examples
  ├── tactic_prefs { tactic_name: score }        # bandit-style
  ├── serendipity                                # 0.0–1.0
  ├── schedule { cron, last_run, next_run, budget }
  ├── created_at, updated_at, version

mindsets/{mindset_id}/versions/{version_id}      # historical rubric
  └── rubric_text, reason_for_change, ts

mindsets/{mindset_id}/candidates/{image_id}      # every image surfaced
  ├── image_url, thumbnail_url, source_page_url
  ├── source_id, discovered_via { agent, tactic, query }
  ├── title, caption, creator, year
  ├── license { name, url, attribution }
  ├── rights_status                              # clear|caveat|unknown
  ├── phash, embedding_id
  ├── judge { score, reason, tags, rubric_version_at_judge }
  ├── status                                     # surfaced|hidden|liked|disliked
  ├── feedback { liked_at?, disliked_at?, note? }
  └── found_at

mindsets/{mindset_id}/hunts/{hunt_id}            # one run
  ├── plan, started_at, completed_at, duration_ms
  ├── n_candidates, n_unique, n_rights_cleared, n_kept
  └── trace_id                                   # for observability

users/{uid}
  └── design_xdm { account_id, api_token } [optional]
```

## The rubric learning loop in detail

The rubric is **plain text** that Gemini follows in every judge call. Why text and not weights? Transparency + editability + cheap iteration.

### Initial rubric

Auto-generated from the theme via Gemini:

> *"Theme: {theme}. Score images by abstract beauty, mood-board readiness, unusual subject, striking colour/form, novelty of composition. Prefer images that surprise designers. Avoid: stock-photo clichés, scientific diagrams, watermarks, low resolution, snapshot quality, anything generic-corporate."*

Tweaked at theme-create time by a brief LLM pass that pulls out theme-specific cues (microscopy → "fluorescence, crystalline structures, micro-scale forms"; brutalist → "raw concrete, monumental scale, geometric severity, harsh light").

### Feedback → rubric update

Triggers:
- Every N likes/dislikes (default N=5), OR
- Explicit user "rethink with this direction:" message

Update step:
1. FeedbackAgent collects: current rubric, last K like/dislike events with images + judge reasoning, any user direction text.
2. Calls Gemini with a reflection prompt: *"Given this rubric, these recent likes (with reasons), these dislikes, and this direction note from the user, propose a revised rubric. Keep it under 200 words, preserve the original theme, surface any newly-emerging motifs."*
3. Saves new rubric as a versioned record. Old few-shot examples gradually retire (most recent 6 kept, mix of positive/negative).
4. Updates tactic-preference scores: tactics that produced liked images get +ε, tactics that produced disliked images get -ε. ScoutAgent reads these next hunt.

### Why this works for the challenge story

The **rubric refinement loop is literal Agent Optimizer behaviour** — programmatic instruction refinement, observable via the rubric version diff in the UI. The challenge calls this out by name in Track 2; we have it as a first-class feature.

## Tactic bandit (a small twist)

ScoutAgent doesn't just pick sources randomly — it has a registry of **tactics**, each with a score that updates from feedback:

| Tactic | Description |
|---|---|
| `prizewinner_archive` | Search competition archives (IOTY, Nikon Small World, Wellcome) |
| `feature_term_query` | Invent technique- or material-specific terms ("differential interference contrast") |
| `adjacent_theme` | Probe near-but-not-target ("scale models" for microscopy) |
| `colour_family_query` | Search by colour palette inferred from liked images |
| `vintage_archive` | Bias toward older / museum / historical sources |
| `negative_space_probe` | Deliberately off-theme (serendipity dial) |
| `creator_pursuit` | Find more work by creators of liked images |
| `temporal_recency` | Bias toward newly-published material |

Each tactic produces queries. After enough feedback, ScoutAgent's plan reflects what works for *this* mindset. This is a much smaller optimisation than the rubric itself but it makes the agent visibly "learn how to look".

## Sources catalogue

See [SOURCES.md](./SOURCES.md) for the full registry. Brief list:

- **Stock with API**: Unsplash, Pexels, Pixabay (keys from xdm_server `.env`)
- **Museum / institution APIs**: Met, Smithsonian, Europeana, NASA (NASA has microscopy/astrophotography subsets), Wikimedia Commons
- **Web search**: Google CSE, Vertex AI Search (grounding), SerpAPI, Brave
- **Competition / IOTY scrapers**: Evident IOTY (already done — `xdm_server/ingest_ioty.py`), Nikon Small World, Wellcome Image Awards, Olympus / NMS archives
- **RSS / image-of-the-day**: APOD, NASA IOTD, NIH NLM image of the week, daily-photo blogs

Each source carries policy metadata: `hotlink_ok`, `attribution_template`, `license_default`, `has_feed`.

## Rights & provenance — the rules we follow

1. **Never download** an image to our storage unless the licence explicitly permits redistribution. We hot-link from the source CDN, mirroring the Pinterest / IOTY ingestor pattern already used in `xdm_server`.
2. **Always preserve attribution.** Every candidate carries `license.attribution` formatted per the source's requirement. The UI renders it next to the image.
3. **Drop on ambiguity.** If we can't confidently identify the licence, the candidate never reaches the user. (User can opt in to a `weak_rights` mode that surfaces them flagged.)
4. **Respect robots.txt** when scraping competition pages (Nikon, Wellcome, etc.) — `httpx` + `urllib.robotparser` check before fetching.
5. **Surface a rights audit** in the UI: per-image licence, per-collection licence summary, downloadable CSV of attributions.

## Deployment topology

```
Cloud Run (web)         ← FastAPI + minimal HTML UI
   │
   ├── reads/writes ─► Firestore (state)
   ├── triggers ─────► Cloud Run Job (hunt worker)
   │                      │
   │                      └── reads/writes ─► Firestore
   │                          calls ────────► Vertex AI (Gemini)
   │                          calls ────────► Source APIs (Unsplash etc.)
   ├── grounds via ──► Vertex AI Search
   └── A2A endpoint ─► external agents

Cloud Scheduler ──► Cloud Run Job (one job per mindset, or shared with mindset_id arg)

Secret Manager ──► all source API keys + Gemini API key + xdm_server API token
Cloud Logging + Cloud Trace ──► observability
```

## Local dev story

You don't need GCP to develop. The codebase has:

- An in-memory `Firestore`-shaped backend (`src/xdm_agent/storage/memory.py`)
- A stubbed Gemini client (`tests/fakes.py`) — returns deterministic scores
- A FastAPI app you can `uvicorn` locally
- The source tools use real keys from `.env` (copied from xdm_server) — so the *hunting* part works without GCP

When you do connect GCP (see [BLOCKERS.md](./BLOCKERS.md)), the same code switches via `STORAGE_BACKEND=firestore` and `GOOGLE_GENAI_USE_VERTEXAI=true`.

## Milestones (8 days)

> Aggressive but real. I'll work in increments and check in.

| Day | Deliverable | Owner |
|---|---|---|
| **D1 (now)** | Plan docs, scaffold, source registry, models, in-memory storage, FastAPI stubs, no real LLM yet | Claude |
| **D2** | All source tools wired (Unsplash, Pexels, Pixabay, Met, Smithsonian, Europeana, NASA, Wikimedia, Google CSE, SerpAPI). Integration tests against real APIs. Competition scraper for Evident IOTY (port from `ingest_ioty.py`). | Claude |
| **D3** | JudgeAgent on Gemini Vertex (real). Initial rubric generator. RightsAgent. DedupeAgent. End-to-end local hunt working. | Claude |
| **D4** | FeedbackAgent + rubric reflection. Tactic bandit. ScoutAgent that reads tactic prefs. Versioning. | Claude |
| **D5** | Cloud Run deploy (real). Cloud Scheduler. Firestore in place. End-to-end working on the cloud. | John + Claude |
| **D6** | Minimal but slick UI (Vite + React or static HTML). Trace viewer. Rights audit page. | Claude |
| **D7** | Eval harness (synthetic personas), Agent Simulation writeup, A2A endpoint, design xdm publish endpoint. | Claude |
| **D8** | Demo video, architecture diagram, submission writeup. Final polish. | John + Claude |

## How I'll work

Each milestone:

1. I implement.
2. I write a brief "REVIEW.md" entry — what I built, what to test, what's incomplete.
3. You sanity-check, give feedback, point out misses.
4. I refine.

If I get blocked on something you uniquely own (GCP setup, API keys, decisions), I'll bookmark it in [BLOCKERS.md](./BLOCKERS.md) and move on to whatever I can do unblocked.

## Open design questions I'll defer

Marked here so we revisit:

- **Mindset sharing / forking** — is there a social layer (one user's mindset visible to another)?
- **Image generation** — explicitly out of scope for v1. Revisit if compelling.
- **Multi-user accounts** — single-user dev, then layer Firebase Auth for v1.
- **Pricing / quotas** — not for the challenge submission; sketch only.
- **Mobile** — desktop web only for the demo.

## What "done" looks like for the submission

- [ ] Public repo with clean README + LICENCE
- [ ] Deployed Cloud Run URL anyone can hit
- [ ] At least one persistent demo mindset hunting on a schedule
- [ ] 3-minute demo video
- [ ] Architecture diagram (this doc has the start of one)
- [ ] Devpost writeup (problem, solution, tech, learnings, business case)
- [ ] Eval results table (precision@10 per persona, rubric convergence)
- [ ] A2A endpoint demonstrated calling out and being called
