# xdm_agent

> An autonomous, taste-learning visual curator. Give it a theme; it hunts the web for the most striking, mood-board-worthy images, judges each against an evolving rubric, learns from your feedback, and keeps checking back as new work appears in the wild.

Built for the **Google for Startups AI Agents Challenge** (Track 2 — Optimize), submission deadline **2026-06-05**.

Sibling to `xdm_server` (FastAPI backend) and `xdm_client` (Next.js frontend), but **self-contained**: this is its own product, not a port. Users with a Design XDM account can optionally publish a curated collection as a mood board there.

## What it does

1. **You give it a mindset.** A short theme phrase: *"microscopy"*, *"brutalist concrete"*, *"60s op-art"*.
2. **It hunts.** A scout agent picks sources (stock libraries, museum APIs, competition archives, web search) and invents evocative, non-literal queries. Searches run in parallel.
3. **It judges.** A Gemini multimodal judge scores every candidate against a rubric written for *your* mindset.
4. **It surfaces a curated feed.** Each image arrives with its source, photographer/creator, licence, and the agent's reasoning for picking it.
5. **It learns.** You like / dislike / nudge in plain language. The rubric is rewritten via Gemini reflection. The agent's taste evolves with yours.
6. **It keeps watching.** Schedule it daily / weekly. Cloud Scheduler wakes the workflow; it returns with whatever the world produced while you were away — a new IOTY winner, a fresh Image of the Day, a new Unsplash photographer.
7. **It respects provenance.** Images are hot-linked, never re-hosted. Every attribution string is preserved. Anything without a clear licence is suppressed by default.

## Why it qualifies for the challenge

| Requirement | How |
|---|---|
| **Mandatory: Gemini API** | All reasoning (scout, judge, rights, feedback) runs on Gemini via Vertex AI |
| **Mandatory: ADK orchestration** | The two LLM decision points — **Scout** (hunt planning) and **Judge** (per-image scoring) — execute as `google.adk` `LlmAgent`s driven by a `Runner`, exposed together as an ADK `SequentialAgent` (`xdm_curator`). The deterministic stages between them (parallel search, dedupe, rights, curate) are sequenced in Python by `run_hunt`. |
| **Mandatory: Cloud Run deployment** | Container deployed on Cloud Run; periodic hunts via Cloud Scheduler → Cloud Run Job |
| Multi-agent emphasis | Two reasoning agents run on ADK — **Scout** (planning) and **Judge** (scoring) — coordinated by an ADK `SequentialAgent`. The pipeline's other concerns (search fan-out, dedupe, rights, curation, feedback reflection) are single-responsibility Python stages sequenced by `run_hunt`. |
| Grounding / RAG | Vertex AI Search grounding for discovery; rubric reflection is RAG over the mindset's feedback history |
| Agent Simulation | Synthetic-persona eval harness (`/eval`) runs the full loop against oracle "good/bad" image sets |
| Agent Observability | Every decision (scout plan, source choice, judge score+reason, rubric change) is traced and rendered in the trace viewer |
| Agent Optimizer | Rubric refinement loop *is* programmatic instruction refinement — the system literally optimises its own prompts |
| Business case | Continuous design inspiration as a service; embeddable; surface area for B2B (agency mindsets, brand-team feeds) |
| A2A interoperability | `POST /a2a/curate` exposes the workflow to other startups' agents |

## Architecture (at a glance)

```
        ┌──────────────┐
USER ─► │  MindsetAgent│  owns rubric + tactic prefs + feedback history
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  ScoutAgent  │  picks sources + invents queries
        └──────┬───────┘
               ▼  (parallel fan-out — Python ThreadPoolExecutor)
   ┌────────┬──┴────┬──────────┬───────────┐
   ▼        ▼       ▼          ▼           ▼
 Stock   Museum   Web      Competition  Vertex AI
 sources sources  search    scrapers     Search
   │        │       │          │           │
   └────────┴───────┼──────────┴───────────┘
                    ▼
            ┌────────────┐
            │  Dedupe    │  url canon. + perceptual hash + embedding sim.
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │  Rights    │  licence + attribution lookup, ambiguous = drop
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │  Judge     │  Gemini multimodal, score vs current rubric
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │  Curator   │  threshold + diversity, persist to collection
            └─────┬──────┘
                  ▼
              USER feed ─► likes / direction shifts ─► FeedbackAgent
                                                          │
                                                          ▼
                                                  rewrites rubric
                                                  (versioned)
```

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the detailed component breakdown and [PLAN.md](./PLAN.md) for the build plan.

## Status

🚧 Planning + scaffold stage. See [PLAN.md](./PLAN.md) for milestones and [BLOCKERS.md](./BLOCKERS.md) for what I'm waiting on.

## Repository

This will be its own public Git repo (a requirement of the challenge). The current monorepo placement at `/Users/john/source/xdm/xdm_agent` is for development convenience; the public repo will contain only this directory, with all secrets sourced from environment variables. Nothing proprietary from `xdm_server` is copied in — design xdm integration happens over an HTTP boundary.

## Licence

TBD — likely Apache-2.0 to maximise marketplace reusability. See [BLOCKERS.md](./BLOCKERS.md#licence).
