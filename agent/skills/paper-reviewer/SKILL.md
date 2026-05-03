---
name: paper-reviewer
description: >
  Conduct a formal peer review of a scientific paper Heath did NOT author —
  for a journal he is reviewing for, or as a pre-submission read for a
  collaborator. Six-phase workflow with a Coordinator (segments paper +
  routes), 6 parallel specialist subagents (Methods Auditor, Logic Checker,
  Presentation, Journal Fit, Adversarial Reader, Citation Verifier),
  a Synthesizer, and a Refiner that applies the structural rubric (blocking
  vs advisory tags, 3:1 minor:major cap, action-oriented minor comments).
  Output is a markdown review tuned to Heath's voice via the bundled
  voice.md. Bundle reference content lives at `Shared drives/Blackmon Lab/
  Projects/paper-reviewer/` (agents/, references/, checklists/, voice.md).
  TRIGGER on: "review this paper", "peer review", "referee for <journal>",
  "what do I think of this manuscript", a directory of review materials,
  mention of revision/rebuttal/response-to-reviewers. Distinct from
  `pre_submission_review` (which is for Heath's OWN drafts heading to
  submission, not papers he is reviewing for someone else).
---

# Paper Reviewer (TEALC)

Help Heath produce a thorough, honest peer review of a scientific paper. The
review must reflect **Heath's** judgment in **Heath's** voice — the subagents
are calibration tools to surface things he might miss and to enforce rigor
against published LLM-review failure modes (hallucinated citations, generic
boilerplate, sycophancy). They do not vote on the verdict.

## Why this design

The published research on LLM-generated peer reviews shows three dominant
failure modes, each with documented mitigations the workflow applies:

| Failure mode | Citation | Mitigation in this workflow |
|---|---|---|
| Hallucinated citations and quotes (2.6% of accepted papers carry fabricated refs; LLM reviews are worse) | NeurIPS 2025 | **Citation Verifier** subagent: dedicated grounding pass — every quote verified in the source PDF |
| Systematic sycophancy (AI scored papers higher in 53.4% of pairs vs human review) | Latona 2024 | **Adversarial Reader** subagent: explicit counterweight to the Methods Auditor's neutral-domain read |
| Generic boilerplate that lacks paper-specific grounding (60% baseline for single-shot LLM reviews) | Liang 2024 / D'Arcy 2024 | **Aspect-decomposed multi-agent** with a Coordinator that segments the paper and routes specialists to relevant sections — MARG showed 2.2× more "good" comments under this structure |
| Drift from Heath's voice / committee-vote feel | n/a (project-specific) | **Refiner** + voice-match pass apply the bundled `voice.md` (tuned from real review samples) and structural rubric |

## Input contract

The user points the skill at a **directory** containing all materials for one
review. Typical contents:

- **Main paper PDF** (always present) — the manuscript under review
- **Supplement / SI** (often) — supplementary materials, often where the real
  methods live
- **Response to reviewers** (if it's a revision) — the authors' reply to the
  previous round
- **Previous reviews** (if it's a revision) — the round-1 reviewer comments
- **Cover letter** (sometimes) — the editor-facing pitch

If the user gives a single PDF instead of a directory, treat its parent
directory as the review directory and assume it's a fresh (non-revision)
submission with no supplement.

## Output contract

A single markdown file at `<directory>/<main-paper-stem>_review.md`.
Example: main paper `~/reviews/smith_2026/manuscript.pdf` →
output `~/reviews/smith_2026/manuscript_review.md`.

The full output template is at
`Shared drives/Blackmon Lab/Projects/paper-reviewer/references/output-template.md`.
Read it before writing the final file.

**Optional second output**: tracked-changes `.docx` with a comment layer.
Only produce this if the user explicitly asks ("give me a tracked-changes
version"). Use the `anthropic-skills:docx` skill to generate it from the
markdown review — does NOT replace the markdown file.

## Reference bundle

The TEALC skill is intentionally thin. The substantive content — agent
prompts, voice corpus, checklists, output template — lives in the Drive
bundle and is read on demand:

```
Shared drives/Blackmon Lab/Projects/paper-reviewer/
├── voice.md                                       # Heath's reviewing-voice guide
├── agents/
│   ├── adversarial.md                             # use as-is for Adversarial Reader prompt
│   ├── methods-specialist.md                      # use as-is for Methods Auditor prompt
│   ├── citation-verifier.md                       # use as-is for Citation Verifier prompt
│   ├── synthesizer.md                             # use as-is for Synthesizer prompt
│   ├── refiner.md                                 # use as-is for Refiner prompt
│   ├── skeptic.md                                 # FOLDED INTO Logic Checker (do not run separately)
│   └── generous-reader.md                         # FOLDED INTO Presentation Agent (do not run separately)
├── references/
│   ├── output-template.md                         # final-review structure — read before writing
│   └── elife-vocabulary.md                        # eLife-specific verbiage when reviewing for eLife
├── checklists/
│   ├── general-biology.md
│   ├── statistics.md
│   ├── phylogenetics.md
│   ├── comparative-methods.md
│   └── genomics.md                                # routed by paper topic
└── dev/heath-review-corpus.md                     # raw prose samples (Methods Auditor / Synthesizer / Refiner reference)
```

## Architecture (the merged 8-agent pipeline)

This is the upgrade over the bundle's original 5-stance design. The bundle's
seven agent prompts in `agents/` are reused, but two of them
(`skeptic.md`, `generous-reader.md`) are folded into the new specialist
roles rather than run as standalone passes — saves two LLM calls without
losing coverage.

```
Phase 1: Coordinator (Sonnet, in-line — you do this)
  • Read main PDF + any supplement / response-to-reviewers.
  • Determine paper type (research / methods / review / theory).
  • Identify journal (from cover letter, formatting, or ask user).
  • Identify topic — pick the matching checklist from `checklists/`.
  • Segment paper: Intro / Methods / Results / Discussion / Figures / Refs.
  • Stash a state.json under `<directory>/.paper-reviewer/state.json` with
    paper-stem, journal, type, topic, checklists picked.

Phase 2: Specialists (DISPATCH IN PARALLEL via spawn_subagent)
  Each subagent receives:
    - The relevant paper section(s) only (segmented by Coordinator)
    - Its agent-prompt file from agents/ (read into the system prompt)
    - The matching checklist from checklists/ (when topic-relevant)
    - Paper metadata (journal, type, topic)

  ┌─ Methods Auditor (Opus, agents/methods-specialist.md)
  │    Sections: Methods, Results (stats), Supplement
  │    Adds: matching checklists/{statistics,comparative-methods,
  │          phylogenetics,genomics}.md as relevant
  │
  ├─ Logic Checker (Sonnet, agents/skeptic.md)
  │    Sections: Intro→Discussion claim/evidence chain only
  │    Job: every claim in Discussion must trace to evidence in Results
  │    DO NOT re-evaluate Methods rigor (Methods Auditor owns that)
  │
  ├─ Presentation Agent (Sonnet, agents/generous-reader.md repurposed +
  │    `voice.md` "presentation" sections)
  │    Sections: full paper
  │    Job: prose clarity, figure quality, structure, what's working well
  │    (the generous-reader stance prevents pure-criticism bias)
  │
  ├─ Journal Fit Agent (Sonnet, no bundle agent prompt — write inline)
  │    Sections: Intro + Discussion
  │    Job: scope match for journal X, audience fit, format compliance
  │    Read references/elife-vocabulary.md if journal is eLife
  │
  ├─ Adversarial Reader (Opus, agents/adversarial.md)
  │    Sections: full paper
  │    Job: actively try to break the paper — fatal-flaw search
  │    This is the explicit counterweight to sycophancy
  │
  └─ Citation Verifier (Sonnet, agents/citation-verifier.md)
       Sections: every cited paper / every direct quote
       Job: verify every reference and quote against the actual cited PDF
       (use fetch_paper_full_text or pdfgetter for cited works as needed)
       This addresses the 2.6% NeurIPS hallucination rate head-on.

  Stash each subagent's report under `<directory>/.paper-reviewer/<role>.md`.

Phase 3: Synthesizer (Opus, agents/synthesizer.md)
  Read all six specialist reports + voice.md + heath-review-corpus.md.
  Produce a draft review following references/output-template.md.
  Heath's stance is the spine — subagent findings filter through it,
  never aggregate into a vote.

Phase 4: Refiner (Opus, agents/refiner.md)
  Apply structural rubric to the synthesis:
  • blocking vs advisory tags on every comment
  • 3:1 minor:major comment cap (kill weakest minors if over)
  • action-oriented minor comments (every minor comment ends in a
    concrete fix the author can apply)
  • mandatory recommendation paragraph (verdict in narrative, not bullet)
  • self-citation why-clauses (any reference Heath would suggest the
    authors add comes with a reason, not just a citation)
  • no generic praise or generic criticism — every line must be paper-
    specific
  • Methods Auditor flags about a checklist item that the paper actually
    handles correctly should be DROPPED, not reported

Phase 5: Voice-match pass (Sonnet, voice.md as system prompt)
  Final rewrite-in-Heath's-voice using voice.md.
  Critical reads: opening paragraph (must be substantive narrative,
  never empty boilerplate), section-header style ("Major issues:" /
  "Other issues:" or "Major Comments" / "Minor Comments"),
  hedge patterns, sentence rhythm.

Phase 6: Write output
  Save the final review to <directory>/<main-paper-stem>_review.md.
  Update state.json phase=complete.
  If user asked for tracked-changes docx: invoke anthropic-skills:docx
  to convert from markdown.
```

## Cost / latency note

Per review (Sonnet 4.6 input ~$3/1M, Opus 4.7 input ~$15/1M as of Nov 2025):

- 1 Coordinator (Sonnet, ~5–10K input) ≈ $0.02
- 4 parallel Sonnet specialists (~5–10K each) ≈ $0.08
- 2 parallel Opus specialists (Methods Auditor, Adversarial) (~10–15K each) ≈ $0.30
- Synthesizer (Opus, ~30K input — six reports) ≈ $0.45
- Refiner (Opus, ~10K input) ≈ $0.15
- Voice-match (Sonnet, ~10K input) ≈ $0.03
- **Total: ~$1.00–1.50 per review** (excludes the paper PDF input cost; small)

Wall-clock with parallel dispatch: **5–10 minutes** depending on Opus
queue. Sequential would be ~25 minutes.

## Resumability

Each subagent's output is stashed under `<directory>/.paper-reviewer/`.
If the user interrupts and resumes, check state.json's phase and skip
already-completed stages. **NEVER re-run the Citation Verifier** — its
output is expensive and deterministic; cache it.

## Common errors to avoid

- **Don't paraphrase the paper into the review.** Quote the relevant
  sentence verbatim and put it in a `> blockquote`. Authors often resent
  reviewers who misread their wording.
- **Don't exceed 5 major comments.** If the synthesizer draft has 8, the
  refiner's job is to pick the 5 that matter most and demote the rest to
  "Other issues."
- **Don't recommend papers Heath would not actually cite.** The Domain
  knowledge to suggest "you should cite X" must come from Heath's own
  reading; the synthesizer can flag a gap, but a real Heath review never
  invents a citation. Tag any suggested addition with a why-clause.
- **Don't compliment the abstract.** Authors didn't write the abstract to
  be reviewed; commenting on it reads as boilerplate.
- **Don't give a verdict without a one-sentence justification.** "Major
  revision" alone is not Heath's style; "Major revision — the analysis
  framework is sound but the [specific issue] needs rebuilding before
  the conclusions hold" is.

## Distinguishing from related skills

- `pre_submission_review` (a tool, not a skill) — for **Heath's OWN drafts**
  before submission. Different rubric (positive framing, find-and-fix). Do
  not confuse.
- `manuscript-polisher` (Anthropic skill) — for editing prose Heath wrote.
  Not a peer review.
- `voice-matching` (TEALC skill) — produces extended prose in Heath's voice.
  This skill USES voice.md but is a workflow, not a prose generator.

## When to ask Heath rather than guess

- Journal not specified anywhere in the materials → ask
- Topic ambiguous (e.g. "Methods paper for general biology audience") → ask which checklists to use
- Heath has reviewed for the journal before with a known stance → ask if any standing instructions
- The paper is by someone Heath knows well → ask about COI / handling
