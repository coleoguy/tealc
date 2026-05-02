---
name: hypothesis-pipeline-rubric
description: >
  Evaluate, propose, or critique a scientific hypothesis or testable claim using
  the lab's type-aware gating pipeline. Use when a chat message or scheduled job
  produces a directional, mechanistic, comparative, methodological, or synthesis
  claim that should be checked for sign coherence, mechanism articulation,
  falsifiability, and novelty relative to prior work. Also use when explaining why
  a hypothesis was blocked or borderline, or when coaching a new claim to meet the
  pipeline bar before submission to the formal pass.
---

# Hypothesis Pipeline Rubric

## When to Use

Trigger this skill whenever:

- A testable claim appears in conversation: directional ("X scales with Y"),
  mechanistic ("A is regulated by B via pathway P"), comparative ("X differs
  between groups"), methodological ("method A outperforms method B"), or
  synthesis ("these two literatures predict the same pattern").
- A proposal from `weekly_hypothesis_generator` surfaces in a briefing and the
  researcher wants to understand why it passed or was blocked.
- The researcher is drafting a hypothesis for a grant Specific Aims page or a
  preregistration and wants a rubric check before running the formal pass.
- A pipeline result needs to be explained to a student or collaborator.

The pipeline is type-aware, not domain-aware. It does not hardcode any particular
biological subdiscipline; the rubric items it applies depend entirely on the
structural shape of the claim (see claim types below).

---

## The Four-Tier Gate

The pipeline in `agent/hypothesis_pipeline.py` runs four stages in sequence.
Each tier has a different cost, model, and blocking criterion.

### Tier 0 — Deterministic Smoke Filter (free, no LLM)

Regex-based. Blocks claims that are:
- Placeholder or smoke-test artifacts (`[SMOKE TEST]`, `<PLACEHOLDER>`,
  `<TO-BE-FILLED>`, `TODO`, `FIXME`, `<INSERT...>`)
- Fewer than 30 characters (too short to be a real hypothesis)
- Accompanied by notes containing `"backfill"`, `"tool validation"`, or
  `"smoke test"`

A claim blocked at Tier 0 costs nothing and produces no LLM call. When a claim
is blocked here, explain to the researcher exactly which token or condition
triggered it, and help them rephrase if the claim is real.

### Tier 1 — Haiku Type Classifier (cheap routing)

Model: `claude-haiku-4-5`. Classifies the claim into one of six types and
returns `confidence`, `claim_summary`, `directional_sign`, `requires_mechanism`,
and `requires_sign_check`. This output selects the rubric items applied in Tier 2.

### Tier 2 — Type-Aware Critic (Sonnet in chat/scheduled; Opus in formal mode)

Model: `claude-sonnet-4-6` (default) or `claude-opus-4-7` (formal mode).

Applies the shared base rubric (claim coherence, reasoning, calibration,
novelty/prior-art) plus the type-specific items defined below.

For directional claims, also emits a structured `causal_graph`
(`claim_cause`, `claim_effect`, `claim_predicted_sign`, `causal_links`) that
feeds directly into Tier 0.5.

### Tier 0.5 — Deterministic Sign Propagation (no LLM)

Runs after Tier 2 for directional claims only. Walks the `causal_graph` from
`claim_cause` to `claim_effect` by composing the signs of each link
(`+`, `-`, or `0`). If the composed sign does not match `claim_predicted_sign`,
the claim is flagged as a sign mismatch — which is a blocking issue.

This check catches cases where the narrative critic assigns a passing score even
though the mechanism, when traced step by step, predicts the opposite direction
from the stated claim.

### Tier 3 — Opus Escalation (borderline only)

Triggers only in `chat` or `scheduled` mode when Sonnet score is 3 and there
are no blocking issues. Uses Opus to re-evaluate. If Opus also scores 3, the
claim passes. If Opus introduces blocking issues, it takes precedence.

---

## The Five Claim Types

### 1. Directional

**Definition.** Claims a direction of effect: "X scales inversely with Y",
"increasing A reduces B", "X is higher in group P than Q". The key feature is
a stated sign (+, -, or ambiguous) that can be checked against the mechanism.

**Rubric items beyond the base:**

- **Sign-coherence (critical).** Trace the proposed mechanism step by step.
  Does it predict the SAME direction as the claim, the OPPOSITE direction, or
  no clear direction? A mechanism that predicts the wrong sign is a fatal flaw
  (blocking issue, score ≤ 2). This check runs both narratively (Tier 2 critic)
  and deterministically (Tier 0.5 sign propagation).
- **Alternative-direction mechanism.** Construct at least one mechanism that
  would predict the opposite sign. If the alternative is comparably plausible
  and the hypothesis doesn't address it, flag in recommendations (not blocking).
- **Structured causal graph.** The critic must emit `causal_graph` with
  explicit nodes and signed links. An empty `causal_links` is itself a blocking
  issue for directional claims.

### 2. Mechanistic

**Definition.** Claims HOW something works, without necessarily asserting a
direction: "X is regulated by Y via pathway P", "A and B interact through C".

**Rubric items beyond the base:**

- **Mechanism articulation.** Is the mechanism stated specifically enough that
  a reader could draw the cause-effect graph? Flag mechanisms that are
  name-only ("via the Wnt pathway") without articulation of actual steps.
- **Alternative mechanism.** Construct at least one alternative that could
  explain the same observation. Flag if comparably plausible and not addressed.

### 3. Comparative / Observational

**Definition.** Claims an observable property of a system, without asserting
a causal mechanism: "Most chromosome counts in beetles fall in the 9–11 range",
"the karyotype dataset is biased toward North American taxa".

**Rubric items beyond the base:**

- **Observable.** What specifically would we see if the claim is true? Flag
  claims that do not specify a measurable outcome.
- **Sampling/ascertainment.** Is there a sampling bias that could produce the
  apparent observation independently of the underlying claim? Flag if not
  addressed (selection bias, study-effort effects, geographic clustering).

### 4. Methodological

**Definition.** Claims about how to do something better: "stochastic mapping
outperforms parsimony for trait inference", "Bayesian phylogenetics is more
reliable for short branches".

**Rubric items beyond the base:**

- **Comparison-to-current.** Does the proposal articulate why the new method
  improves over what is currently used, by name? Flag superiority claims that
  do not engage with current methods specifically.
- **Falsifiability.** How would we know the new method is NOT better? Is there
  a benchmark, holdout, or simulation that could falsify the claim?

### 5. Synthesis

**Definition.** Bridges findings across separate literatures: "the fragile-Y
and meiotic-drive literatures predict the same pattern". The inference that
requires both literatures is the interesting move.

**Rubric items beyond the base:**

- **Bridge-claim.** Does the synthesis make a claim that goes beyond the sum
  of the parts? "X says A and Y says B" is not a synthesis. Flag if the bridge
  inference is absent.
- **Citation grounding.** Both literatures must be cited specifically. Synthesis
  claims live or die on the citations.

### 6. Speculative

**Definition.** Framed as "what if", with no current evidentiary anchor.

**Treatment.** Not held to mechanism or sign-coherence standards. Typical
score 2–3. The standard note: "acceptable as speculation; should not be
promoted to an executable intention without further grounding."

---

## Base Rubric (All Types)

These four criteria apply regardless of claim type:

1. **Claim coherence.** Is the claim stated clearly and unambiguously? Could
   two readers disagree about what is being claimed? Flag vague phrasing and
   unstated referents.

2. **Reasoning.** Does the rationale connect to the claim? Walk through the
   cause-and-effect or evidentiary chain. Flag rationale that merely restates
   the claim or skips a step.

3. **Calibration.** Is the hypothesis stated as a hypothesis (not a
   conclusion)? Are limitations acknowledged? Flag overconfidence and hype
   words: revolutionary, paradigm-shifting, definitively, transformative,
   comprehensive.

4. **Novelty / prior art.** Does the rationale acknowledge what is already
   known? Flag claims that appear to restate prior work without saying so.

---

## Blocking Issues vs. Recommendations

This distinction is load-bearing. The pipeline uses it to decide whether a
claim passes the gate:

**Blocking issues** — fatal flaws that cannot be fixed without rewriting the
core claim or mechanism:
- Sign mismatch (mechanism predicts the opposite direction)
- Mechanism-free reasoning when the type is directional or mechanistic
- Smoke-test or placeholder language
- Unfalsifiable claim as stated
- Claim restates an already-published finding without acknowledgment

**Recommendations** — soft suggestions that improve the claim but do not
invalidate it:
- Specify the statistical method more precisely
- Name the null-model prediction
- Add a proposed sample size
- Acknowledge prior literature that partially overlaps
- Improve the proposed test design

If the hypothesis is fundamentally sound but the test design has caveats, score
it 4 with empty `blocking_issues` and put the caveats in recommendations. Do
not downgrade a sound claim because the method section needs work; the method
can be revised without rewriting the claim.

---

## Scoring Scale

```
5 — well-grounded, specific, falsifiable, modestly stated;
    mechanism/reasoning aligns with claim
4 — strong with minor issues
3 — acceptable but needs tightening (borderline; Opus escalation may fire)
2 — significant flaws (sign mismatch, name-only mechanism, hype, smoke-test feel)
1 — not viable (mechanism predicts the opposite, unfalsifiable, pure speculation
    in a non-speculative type)
```

Gate passes when score ≥ 3 AND blocking_issues is empty.

---

## Falsifiability Requirement

Every hypothesis proposed under this skill must include a falsifying observation:
what specific empirical result would, if observed, require revising or rejecting
the claim? This requirement is not optional.

The `_HYPOTHESIS_SYSTEM` prompt in `weekly_hypothesis_generator.py` encodes it:

> "MUST specify both: (a) what observation would support the hypothesis,
> (b) what observation would falsify it. Falsifiability is required."

When coaching a researcher to improve a hypothesis, ask: "What would you have to
see in the data to conclude you were wrong?" If the answer is "nothing could
falsify it," the claim is not a hypothesis.

---

## Calibration as Habit

Every hypothesis assessment produced under this skill must include three items:

1. **Confidence level** — `low` / `med` / `high`, stated explicitly
2. **Strongest counter-argument** — the best case against the claim, stated in
   the hypothesis's own terms (not a generic caveat)
3. **Falsifying observation** — the specific empirical result that would require
   revising or rejecting the claim

This mirrors the `<uncertainty>` stance in `agent/graph.py`:

> "Calibrate. With every hypothesis or draft, name your confidence (low/med/high),
> the strongest counter-argument you can generate, and one observation or
> experiment that would change your mind."

---

## Wiki Consultation Rule

Before finalizing any hypothesis proposal, the system checks the lab wiki for
existing claims in the same domain. This rule exists because a hypothesis that
restates a paper the lab itself has already published is the canonical failure
mode the pipeline is designed to prevent.

The `weekly_hypothesis_generator` job implements this check by calling
`pick_relevant_wiki_topics` and `read_wiki_topics_block` to inject synthesized
prior findings into the LLM's context before hypothesis generation (lines 308–318
of `agent/jobs/weekly_hypothesis_generator.py`).

In chat mode, when proposing or coaching a hypothesis, apply the same check
manually: before presenting a candidate claim, ask whether the lab's existing
publications already test or establish the core relationship. If you cannot verify
this from context, flag it explicitly: "[check wiki: does an existing result
already address this?]"

The canonical example of the failure this rule prevents: a hypothesis proposing
that a specific evolutionary mechanism drives a specific pattern in a clade the
lab had already studied and published on — a wiki check on the relevant topic
pages would have immediately surfaced the prior finding, and the hypothesis would
either have been refined into a genuine extension or discarded.

---

## Integration with Scheduled Jobs

- `weekly_hypothesis_generator` (`agent/jobs/weekly_hypothesis_generator.py`) —
  runs Sunday 5am Central. Generates 1–2 hypotheses per active project from
  recent literature notes, gates each with `run_pipeline(mode="scheduled")`, and
  creates a briefing. The researcher reviews via `list_hypothesis_proposals` and
  promotes or rejects each one.

- `run_formal_hypothesis_pass` (tool in `agent/tools.py`) — chat-accessible tool
  that runs `run_pipeline(mode="formal")` on a supplied claim. Uses Opus for the
  Tier 2 critic and enables the Europe PMC contradiction check
  (`tier25_contradiction_check`). Use for any claim the researcher is considering
  for a grant aim or preregistration.

- `record_chat_artifact(kind='hypothesis')` — logs a hypothesis surfaced in chat
  and runs `run_pipeline(mode="chat")` on it.

---

## Contradiction Check (Formal Mode Only)

In formal mode, the pipeline additionally queries Europe PMC for papers that
might contradict the claim (`tier25_contradiction_check`). A "contradiction"
requires that the paper's findings argue AGAINST the hypothesis — not merely
fail to support it. Different organism, different scope, or partial overlap is
not a contradiction. If contradicting papers are found, the claim is blocked
and the score is capped at 2.

---

## Pipeline Entry Points

```python
from agent.hypothesis_pipeline import run_pipeline, format_result_md

# Chat mode — Sonnet critic, no contradiction check
result = run_pipeline(content_md=claim_text, mode="chat")

# Formal mode — Opus critic, contradiction check enabled
result = run_pipeline(content_md=claim_text, mode="formal")

# Scheduled mode — Sonnet critic, no contradiction check
result = run_pipeline(content_md=claim_text, mode="scheduled")

# Render for display
print(format_result_md(result))
```
