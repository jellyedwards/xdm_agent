# BLOCKERS — what I need from John

Each item lists *what* I need, *why*, *when* it becomes blocking, and *what I can do meanwhile*.

## Critical-path (block deployment)

### 1. GCP project + service account

**Need:**
- Project ID (existing or new — recommend a fresh one for the challenge, e.g. `xdm-agent-prod`)
- Billing enabled
- Region preference (recommend `europe-west2` London for latency from IE, or `us-central1` if Vertex feature parity matters more)
- APIs enabled: Vertex AI, Generative Language, Cloud Run, Cloud Run Jobs, Cloud Scheduler, Firestore, Secret Manager, Cloud Build, Artifact Registry, Cloud Logging, Cloud Trace
- Service account with: Vertex AI User, Cloud Run Invoker, Cloud Datastore User, Secret Manager Secret Accessor, Cloud Trace Agent — *and* `gcloud auth application-default login` run locally so I can develop against real Vertex when needed

**Why:** Cloud Run + Vertex + Firestore are mandatory.

**Blocks:** D5 onward (deployment milestone). D3 partially — I can develop the judge with the in-process Gemini SDK using a `GOOGLE_API_KEY` from AI Studio while we wait for Vertex.

**Meanwhile:** I'll build with `STORAGE_BACKEND=memory` and `LLM_BACKEND=gemini-api-key`. Add a `GOOGLE_API_KEY` to `.env` (AI Studio key, free) when convenient.

---

### 2. Repository decision

**Need:** Confirm:
- (a) `xdm_agent` becomes its own repo (recommended) — separate `git init`, ready to push to a public GitHub repo when ready
- (b) GitHub owner (your personal account, or a new org for the challenge?)
- (c) Licence — recommend **Apache-2.0** (permissive, Marketplace-friendly per Track 3 future-state)

**Why:** Challenge mandates a public repo. We need clean history with zero secrets in it from minute one.

**Blocks:** Final submission. Not D1-D7.

**Meanwhile:** I'll keep working in `xdm/xdm_agent` on the existing monorepo; the directory is structured so a `git init && git add .` from inside it produces a clean self-contained repo.

---

### 3. Devpost team

**Need:** Confirm you've accepted the invite from `devpost.team/hackathon_guest_invites/...`. If team submission, who's on the team?

**Why:** Submission needs an accepted invite and a Devpost project page.

**Blocks:** Submission.

---

## Soft-blocking (decisions I'd rather you make)

### 4. Product name

**Need:** A name for the agent. Working title is "xdm_agent". Suggestions, all 5-letter-ish, web-friendly:

- **Lens** — implies seeing, taste-driven
- **Curio** — curates curiosities
- **Drift** — implies it works while you sleep
- **Quarry** — what it hunts is the quarry
- **Find** — the simplest name imaginable
- Keep **xdm/Inspire** or similar to stay in family

This shapes the Devpost title, repo name, domain (if any), and UI copy.

### 5. design xdm integration scope

Two options for the "publish to design xdm" feature:

- **(a) Real:** I add a `POST /board_from_external_agent` endpoint to `xdm_server` that accepts the curator's JSON and creates a board for the authenticated user. ~2 hours of `xdm_server` work, but a *real* B2C feature you'd keep.
- **(b) Mock:** UI button shows "Published — view in design xdm" but actually just links to a mocked board URL. Saves time; demo-only.

**Recommendation:** (a) — it's both a clean demo and a real product feature. I'll keep `xdm_server` changes minimal and behind a feature flag.

### 6. API key strategy

**Need:** Confirm we can reuse the existing `.env` keys (Unsplash, Pexels, Pixabay, Google CSE, SerpAPI, Vertex search, Freepik, Gemini) for the new project's dev *and* production. Alternative: register a fresh set so the projects can be billed and rate-limited independently.

**Recommendation:** Reuse for now; if anything goes prod-noisy, split.

### 7. Vertex AI Search app

**Need:** Confirm we can reuse `GOOGLE_VERTEX_SEARCH_APP_ID` from xdm_server, or create a new one focused on "inspirational/editorial imagery" with tuned ranking signals.

**Recommendation:** Reuse for now; revisit if quality issues surface.

### 8. Schedule + budget defaults

**Need:** Default cron and cost cap for a mindset's scheduled hunts. Recommend: daily at 06:00 UTC, max 100 candidates judged per run, ~$0.10/run cap.

---

## Things only you can do

| # | Task | When |
|---|---|---|
| A | Accept Devpost invite & create the Devpost project page | Any time |
| B | Create GCP project & enable APIs (or share credentials so I drive `gcloud`) | Before D5 |
| C | `gcloud auth application-default login` on this Mac | Before any real Vertex call |
| D | Initial `git init` + push to GitHub (when we're ready to public) | Before submission |
| E | Record the 3-min demo video | After D7 |
| F | Final review of the submission writeup | D8 |
| G | (Optional) Register a domain — `lens.designxdm.com` or similar — for the demo URL | Optional polish |

## Decisions I'll make myself unless you object

I'll proceed on these as the default, and you can countermand:

- **ADK as orchestrator** (not LangChain/CrewAI). Aligns with challenge's primary recommendation.
- **Gemini via Vertex** (not Generative Language API). Better for the challenge stack story and cost.
- **Firestore** for state (not Cloud SQL). Right shape, free tier, simplest.
- **Cloud Run service + Cloud Run Job** topology (not GKE). Cheaper, simpler, equally compliant.
- **Vanilla JS + a single HTML page** for the demo UI (no Next.js build). Fastest path. If you'd rather a Next.js client, say so and I'll switch (it'll cost ~half a day).
- **Apache-2.0 licence**.
- **Python 3.11+** (ADK requirement).
- **Hot-link, never re-host** policy for all imagery, with rights audit + drop-on-ambiguity defaults.

## What I will NOT do without explicit OK

- Commit any secrets, ever
- Push to a public GitHub repo
- Make production API calls that incur > $1 of spend
- Modify `xdm_server` or `xdm_client` (the publish endpoint in §5 only if you green-light option (a))
- Send any user-facing email or webhook
- Set up paid Google services that aren't already enabled
