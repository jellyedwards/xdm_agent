# How the rubric works

> The rubric is the agent's *taste*. It starts as a generic template, gets specialised at mindset creation, and rewrites itself from your feedback.

## Anatomy of a rubric

A rubric is a plain-text instruction Gemini reads on every judge call. It has loosely-enforced sections:

```
THEME: <one line, immutable per mindset>

POSITIVE — score higher for:
- <bullet>
- <bullet>
- ...

NEGATIVE — score lower for:
- <bullet>
- <bullet>
- ...

NOTES — current preferences from feedback:
- <bullet>
- <bullet>

EXAMPLES — for reference only, do not copy:
- LIKED: <image_url>  — "<short reason from user or judge>"
- LIKED: ...
- DISLIKED: ...
```

The judge prompt asks Gemini to:
1. Briefly describe the image (one sentence).
2. Apply the rubric.
3. Return JSON: `{score: 0-10, reason: str, tags: [str, ...]}`.

## Initial rubric (mindset creation)

When a user creates a mindset (e.g. `theme="microscopy"`), the system runs a one-shot Gemini call to seed the rubric:

```
You are seeding a curation rubric for a designer's mood board on the
theme "{theme}".

Produce a rubric in this format:
[FORMAT BLOCK]

Rules:
- Theme line is exactly "{theme}" — do not paraphrase.
- 4-7 positive bullets emphasising abstract beauty, unusual subjects,
  unusual composition, technique-specific aesthetic markers for this
  theme.
- 4-6 negative bullets covering cliché stock-photo treatments,
  textbook diagrams, watermarks, low res, snapshot quality.
- Leave NOTES empty.
- Leave EXAMPLES empty.
```

The system stores this as version 1.

## Feedback events

The UI captures three kinds of feedback:

1. **Like / dislike** on an image (binary, with optional short note)
2. **Hide** (soft-dislike: "not for this collection but don't penalise")
3. **Direction text** ("now lean into bioluminescence", "stop showing me single-cell stuff")

All three are appended to `mindsets/{id}/feedback/` with ts, image_id (where applicable), note.

## Reflection (rubric rewrite)

Triggered:
- Every N=5 feedback events, OR
- Immediately when a `direction` event arrives, OR
- Manually via "Refresh rubric" button

Prompt sketch:

```
You are refining a curation rubric based on recent user feedback.

CURRENT RUBRIC:
{rubric_text}

RECENT FEEDBACK (most recent first):
- LIKED at {ts}: {image_url} — judge had said "{judge_reason}" — user note: {note?}
- DISLIKED at {ts}: {image_url} — judge had said "{judge_reason}" — user note: {note?}
- DIRECTION at {ts}: "{direction_text}"
- ... (up to 20)

TASK:
Rewrite the rubric so future judging better reflects what the user
likes. Preserve the THEME line verbatim. Keep total length under 250
words. Update NOTES to capture any new emerging preferences in
plain English. Refresh EXAMPLES with up to 4 LIKED and 4 DISLIKED
from the feedback (most recent first), with the reasons in plain
English. Do NOT make the rubric narrower than the user has actually
indicated — if uncertain, keep things open.

Output the new rubric only.
```

The new rubric is saved as `mindsets/{id}/versions/{ts}` with a diff against the prior version and a `reason_for_change` field summarising what shifted.

## Tactic bandit update

Separately from the rubric, tactic_prefs update on every like/dislike:

```python
# in FeedbackAgent
def update_tactics(mindset, event):
    img = candidate_for(event.image_id)
    tactic = img.discovered_via.tactic
    if event.kind == "like":
        mindset.tactic_prefs[tactic] += LIKE_BONUS  # e.g. 0.1
    elif event.kind == "dislike":
        mindset.tactic_prefs[tactic] -= DISLIKE_PENALTY  # e.g. 0.05
    # Bound to [-1.0, 2.0]; decay toward 1.0 over time
```

ScoutAgent reads `tactic_prefs` to weight which tactics to deploy in the next hunt plan. We start with all tactics at 1.0 (neutral). Over feedback, the mindset's "personality" of how-to-hunt emerges.

## Versioning + accountability

Every judge result records `rubric_version_at_judge`. So when a user looks at why a 3-month-old image got into the collection, the trace shows them the rubric *as it was then*. The mindset evolves but the audit trail stays intact.

## Few-shot examples

The EXAMPLES block is *not* used as in-context Gemini examples (we keep the prompt short for cost). Instead, the rubric's NOTES section captures the *summary* of what those examples taught. The EXAMPLES list is for human-facing display and for retraining if we ever swap models.

(Future: if quality plateaus, switch to few-shot inclusion + larger context window.)

## What this maps to in the challenge

The reflection loop is exactly the **Agent Optimizer** story: programmatic refinement of agent instructions based on observed performance. The challenge calls this out as a Track 2 focus area, and we have it as a first-class feature with versioning, diffs, and observability — not bolted on.
