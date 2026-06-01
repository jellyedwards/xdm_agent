# xdm_agent — Architecture

## High-level

```
                          ┌────────────────────────────────────┐
                          │                USER                 │
                          │  (browser UI or external agent A2A) │
                          └─────────────┬──────────────────────┘
                                        │
                          ┌─────────────▼──────────────────────┐
                          │      FastAPI on Cloud Run           │
                          │                                     │
                          │  /mindset, /feedback, /hunt,        │
                          │  /collection, /a2a/curate           │
                          └─────────────┬──────────────────────┘
                                        │
            ┌───────────────────────────┼───────────────────────────────┐
            │                           │                                │
            ▼                           ▼                                ▼
   ┌────────────────┐          ┌────────────────┐              ┌────────────────┐
   │ run_hunt       │          │ FeedbackAgent  │              │ PublisherAgent │
   │ (Python; calls │          │  (rubric LLM)  │              │ (A2A surface)  │
   │  ADK reasoning)│          └────────┬───────┘              └────────────────┘
   └────┬───────────┘                   │
        │                               │
        │ scout → search → dedupe       │ reflect → version
        │  → rights → judge → curator   │
        ▼                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │                       Firestore                             │
   │                                                             │
   │  mindsets/  versions/  candidates/  hunts/  users/          │
   └────────────────────────────────────────────────────────────┘

   Tools layer (plain Python functions, called directly):
   ├── Source tools (Unsplash, Pexels, Pixabay, Met, Smithsonian,
   │   Europeana, NASA, Wikimedia, Google CSE, Vertex AI Search,
   │   SerpAPI, Brave, Evident IOTY scraper, Nikon Small World scraper)
   ├── Dedup tools (URL canon, phash, embedding similarity)
   ├── Rights tools (registry lookup + page-level licence detection)
   └── Notify tools (digest email/webhook, design xdm publish)

   Triggers:
   └── Cloud Scheduler → Cloud Run Job → run_hunt(mindset_id)
```

## Layout

Matches xdm_server's style: flat sibling files, no nested packages. Functions live in whichever file is the primary surface for that concern.

```
xdm_agent/
├── main.py             # FastAPI app, routes, run_hunt wiring, agent runtime entrypoint
├── agents.py           # The hunt pipeline: ADK LlmAgents for the two reasoning nodes (scout, judge) + a SequentialAgent root (xdm_curator); the deterministic stages (search, dedupe, rights, curator, feedback) as plain Python; run_hunt sequences them
├── sources.py          # Source registry + every source query function (Unsplash, Pexels, Pixabay, Met, Smithsonian, Europeana, NASA, Wikimedia, Google CSE, Vertex Search, SerpAPI, Brave, IOTY scraper, Nikon Small World scraper) + scraping helpers
├── storage.py          # Pydantic models (Mindset, Candidate, Hunt, FeedbackEvent) + Firestore backend + in-memory backend, picked by STORAGE_BACKEND env var
├── rubric.py           # Initial rubric seeding, reflection (rewrite from feedback), tactic bandit update, versioning
├── dedup.py            # URL canon, perceptual hash, embedding similarity (only split out because it has heavier deps — PIL, imagehash)
├── ui/                 # Static HTML + vanilla JS for the demo UI (no build step)
│   ├── index.html
│   ├── mindset.html
│   ├── trace.html
│   └── app.js
├── eval.py             # Synthetic-persona harness + metrics
├── personas/           # JSON persona briefs for eval
├── oracles/            # Ground-truth liked/disliked image URLs per persona
├── Dockerfile
├── cloudbuild.yaml
├── requirements.txt
├── .env.example
├── .gitignore
├── .gcloudignore
├── README.md
├── PLAN.md
├── ARCHITECTURE.md
├── BLOCKERS.md
├── SOURCES.md
└── RUBRIC.md
```

## Components

The stages below live as functions inside `agents.py` (feedback in `rubric.py`). Only the two reasoning nodes — **Scout** and **Judge** — run on ADK as `LlmAgent`s; everything else is plain Python. `run_hunt` is the canonical sequencer.

| Stage | Runs on | Role |
|---|---|---|
| `MindsetAgent` | Python | State controller, loads/saves Mindset |
| **Scout** | **ADK `LlmAgent`** | Plans a hunt: chooses sources + queries from tactic prefs (`output_schema=HuntPlan`) |
| `execute_searches` | Python (`ThreadPoolExecutor`) | Parallel fan-out to source functions |
| `dedupe_candidates` | Python | URL/phash/embedding dedup |
| `apply_rights` | Python | Licence + attribution determination |
| **Judge** | **ADK `LlmAgent` (multimodal)** | Gemini scores each candidate (`output_schema=JudgeResult`) |
| `curate` | Python | Diversity-aware ranking + persistence |
| `FeedbackAgent` | Python + Gemini (`rubric.py`) | Rubric reflection + tactic update |
| `SchedulerAgent` | Python | Cron config + hunt budgeting |
| `PublisherAgent` | Python | A2A surface |
| `xdm_curator` | ADK `SequentialAgent` | Discoverable root composing Scout → Judge (for `adk web`) |
| `run_hunt` | Python | Top-level sequencer wiring the hunt pipeline |

### Tool signatures

Source query functions in `sources.py` share a uniform signature:

```python
def search_unsplash(mindset, query, n=10): ...
def search_pexels(mindset, query, n=10): ...
# etc.
```

They return a list of `Candidate` records. `execute_searches` in `agents.py` calls them directly across a `ThreadPoolExecutor` — they are not wrapped as ADK `FunctionTool`s (the Scout plans *which* sources to call; the calling itself is plain Python).

### Storage

`storage.py` exposes `get_store()` which returns either a `FirestoreStore` or `MemoryStore` based on the `STORAGE_BACKEND` env var. Both implement the same Python protocol — load/save mindset, append candidate, list candidates, log hunt, list hunts.

## Data flow — a hunt, in detail

1. **Trigger** — Cloud Scheduler hits `/hunt/{mindset_id}` (or user clicks "Hunt now"). `run_hunt` sequences the steps below.
2. **MindsetAgent** loads the mindset from Firestore (rubric, tactics, recent collection).
3. **Scout** (ADK `LlmAgent`, via `InMemoryRunner`) is called with mindset + tactic_prefs + recent hunt summaries; its `output_schema=HuntPlan` returns 5–15 `{source_id, query, why}` entries.
4. **`execute_searches`** (plain Python, `ThreadPoolExecutor`) invokes each source function with its query. Each returns raw candidates (image_url, source_page_url, captured metadata).
5. **`dedupe_candidates`** canonicalises URLs, computes phash (lazy fetch of thumbnail), filters intra-batch dupes and known existing items.
6. **`apply_rights`** looks up each source in the registry. For ambiguous sources (e.g. web search hits) it fetches the page and tries to detect a licence (image_meta, common embed patterns, Creative Commons markers). Drops anything unresolved.
7. **Judge** (ADK `LlmAgent`, multimodal) scores each candidate — `Part.from_uri(image)` + rubric → `JudgeResult` (score+reason+tags). Run across a `ThreadPoolExecutor`, concurrency capped to respect quotas.
8. **`curate`** applies score threshold, diversity penalty (penalise candidates close in embedding to already-kept), and ranks.
9. **Persistence** — kept candidates land in `mindsets/{id}/candidates/`. A `Hunt` record summarises the run.
10. **Notify** — user gets a UI badge / optional email digest.

Each step's inputs, outputs, durations, and reasoning are recorded to a structured trace (Cloud Logging + a per-hunt trace JSON in Firestore). The trace viewer in the UI renders it as a tree.

## A2A surface

```
POST /a2a/curate
{
  "agent_id": "calling-agent-id",
  "brief": "60s op-art for an interior brand refresh",
  "n": 10,
  "max_seconds": 30
}
→
{
  "candidates": [{
    "image_url": "...",
    "source_page_url": "...",
    "title": "...",
    "creator": "...",
    "license": {...},
    "attribution": "...",
    "judge_score": 8.4,
    "judge_reason": "..."
  }, ...],
  "trace_id": "..."
}
```

This is the hook that turns the agent from a B2C tool into something an external startup's agent can call ("hey, I need 10 mood images for X, with rights cleared"). It's also the demo lever for the challenge's A2A interoperability requirement.

## Observability

- **Cloud Logging** receives structured logs from every agent step with `trace_id`, `mindset_id`, `hunt_id`.
- **Cloud Trace** spans wrap each agent invocation.
- **Per-hunt trace JSON** stored in Firestore is the source of truth for the in-UI trace viewer. (Cloud Logging is for ops; the trace viewer is for product.)
- **Rubric version diffs** — every rubric change saved as a diff, viewable from the UI.

## Security

- All API keys in **Secret Manager**; mounted as env vars in Cloud Run.
- Per-user auth via **Firebase Auth** (reuses xdm_server's pattern; later milestone, single-user for the demo).
- `xdm_server` publish endpoint authenticates via short-lived OAuth token from the user.
- A2A endpoint requires an agent identity header (HMAC or eventual mTLS); rate-limited per agent.
- `robots.txt` checked before any scraping.

## Cost shape (rough)

- Gemini judge: 1 call per candidate, ~$0.001 each → 100 candidates / hunt → ~$0.10/hunt.
- Vertex AI Search: per-query pricing, ~$0.005 → negligible.
- Cloud Run: idle-to-zero scaling, ~free for demo traffic.
- Firestore: well within free tier.

A daily hunt on a single mindset is well under a dollar a month. The $500 challenge credits cover the demo and a long evaluation period.
