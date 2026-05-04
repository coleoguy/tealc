---
name: pre-submission-review
description: >
  Pre-submission review of a manuscript Heath authored or co-authored,
  before it goes to a journal or funder. The output is ALWAYS a Google
  Doc copy of the manuscript with visible inline review markup (red
  strikethrough for old text + red insertion for new text — both visible
  side-by-side) plus margin comments. NEVER a journal-style prose review
  written into a fresh blank doc. Same multi-agent architecture as
  paper-reviewer (Coordinator → 6 parallel specialists → Synthesizer →
  Refiner) with three critical inversions: stance is INTERNAL ADVISOR
  ("find issues so the author can fix them, not so a reviewer can score
  them"), voice is HEATH'S MANUSCRIPT voice (NOT peer-review voice),
  output is a marked-up copy via mark_changes_in_google_doc +
  insert_comment_in_google_doc on a doc made by copy_google_doc.

  TRIGGER on any phrasing where Heath wants HIS OWN manuscript reviewed
  before submission. All of these route here:
    "review my paper before I submit"
    "pre-submission review"
    "review the <topic> manuscript" / "review the manuscript for <project>"
    "give me a critique of my draft"
    "what would reviewers attack"
    "look this over before I send to <journal>"
    "I'm sending this to <journal> next week, look it over"
    Any review request where the manuscript lives in Heath's
    `Projects/` folder structure (vs. a `~/reviews/<journal>/` directory)

  Distinct from `paper-reviewer` (which is Heath reviewing SOMEONE
  ELSE's paper that came in via a journal — materials typically in
  `~/reviews/<journal>/`, NOT in `Projects/`). **Default rule when
  ambiguous: if the source file lives in any subdirectory of Heath's
  `Projects/` folder, use this skill, not paper-reviewer.** Distinct
  from the legacy `pre_submission_review` tool (single-shot, returns
  markdown — kept only for "quick gut check" requests on inline-pasted
  text without a file path).
---

# Pre-Submission Review (TEALC)

## ⚠ FIRST READ — call ONE TOOL, do not assemble the workflow yourself

**Use the tool `run_pre_submission_review(project, venue)`** for ANY
pre-submission review request. That single tool deterministically runs
the entire 5-specialist + synthesizer + copy + markup + comments
pipeline in Python — guaranteed to produce a marked-up Google Doc copy,
guaranteed never to fall through to a journal-style prose review.

The `project` parameter accepts:
  - A project ID like `p_002`
  - A project name like `"Achiasmy Synthesis"`
  - A name fragment like `"achiasmy"`

The orchestrator calls `find_project_manuscript(project)` internally
to locate the current manuscript at `<project>/manuscript/*Manuscript*.gdoc`
— no Drive search loop needed, no asking Heath for paths. Heath's
projects follow a strict convention (current manuscript lives in the
`manuscript/` subdirectory; `deprecated/` and `_dev/` are excluded;
the most-recently-edited file wins), and the finder respects it.

```
# Most natural call — all that's usually needed:
run_pre_submission_review(project="Achiasmy Synthesis", venue="Am_Nat")

# If for some reason the project finder picks the wrong file, fall
# back to passing an explicit path:
run_pre_submission_review(
    manuscript_path="/Users/.../Projects/<dir>/manuscript/<file>.gdoc",
    venue="Am_Nat",
)
```

If the user mentions a project by name without a path, **DO NOT**
search Drive — call `find_project_manuscript(project)` first to
preview which file will be used, OR just call
`run_pre_submission_review(project=...)` and let the orchestrator
locate the manuscript.

Do NOT try to assemble this workflow yourself by calling
`spawn_subagent` + `copy_google_doc` + `mark_changes_in_google_doc`
individually. The LLM-driven multi-step approach has been observed in
production to skip steps and fall back to single-shot prose review,
producing a copy with no markup. The orchestrator tool exists
specifically to remove that failure mode.

The rest of this SKILL.md describes the architecture for understanding
and customization. The default action when a user asks for a
pre-submission review is one tool call.

## ⚠ MUST READ — output enforcement

The output of this skill is **ALWAYS** a Google Doc copy of Heath's
manuscript with visible inline markup + margin comments. There are
exactly two output paths (workflow A primary, workflow A-fallback if
the markup tool is unavailable for any reason). Both produce a
**marked-up copy of the manuscript itself**.

**Do NOT write a fresh Google Doc with a journal-style prose review.**
That is what the `paper-reviewer` skill produces, and it is the wrong
output for pre-submission. If the multi-agent pipeline succeeds but
you cannot produce inline markup or comments on a copy, STOP and ask
Heath for guidance — do not silently fall through to a prose-review
document.

If subagent dispatch fails (cost-tracking error, parallel-call error,
etc.): degrade to a **sequential** specialist pass on the same agent
prompts (you, the chat agent, plays each specialist role one at a time
in-context), then proceed with the SAME output workflow (copy + markup
+ comments). Sequential pass is slower but produces the right output
shape. Do NOT use subagent failure as a reason to skip the markup step.

## What you do

Help Heath find every problem reviewers will find — before reviewers find
them. The output is a marked-up copy of Heath's manuscript where every issue is
either a one-click-acceptable tracked change or a margin comment with a
clear ask.

## Why this design

This is not peer review for a journal — it's adversarial self-review on
behalf of the author. The failure modes to avoid are different:

| Failure mode | Why it bites pre-submission specifically | Mitigation |
|---|---|---|
| Sycophancy on Heath's own work | The agent has been working with Heath all session — natural pull to flatter | **Adversarial Reader (Opus)** explicitly prompted with "you are reviewer 2 from hell — find every fatal flaw" |
| Generic "tighten the prose" comments | Heath can do that himself; he wants the issues HE missed | Each finding must include a verbatim location quote and a specific reason |
| Suggesting prose that doesn't sound like Heath | Tracked changes that reviewers see WILL get accepted; agent prose merged in is durable damage to voice | **Voice-match pass uses the `voice-matching` skill** (manuscript voice from `voice_index.py`), NOT the paper-reviewer skill's `voice.md` (which is peer-review voice — wrong context) |
| Wrong venue rubric | Nature-tier prose suggestions on an Am Nat paper waste effort | Coordinator identifies venue first; Journal Fit Agent uses venue-specific criteria |

## Input contract

User points the skill at a **manuscript draft** plus a **target venue**.
Three input formats supported, in preference order:

1. **`.docx` of the manuscript** (best — output can be a true tracked-changes
   clone of the input)
2. **`.pdf` of the manuscript** (degraded — output docx will be re-typeset
   from extracted text; formatting lost; tracked changes still work)
3. **`.md` or `.tex` of the manuscript** (rare — convert to docx as Phase 0)

**Target venue is required.** If user didn't specify, ask. Known venue
rubrics (mirrored from the legacy `pre_submission_review` tool):

- `journal_generic`
- `nature_tier` (Nature, Science, Cell — short, broad, structured abstract)
- `MIRA_study_section` (NIH MIRA — innovation framing, 5-yr arc)
- `NSF_DEB` (NSF Division of Environmental Biology — Intellectual Merit + Broader Impacts)
- `google_org_grant` (deliverable framing, public-good measurability)
- `Am_Nat`, `Evolution`, `MBE`, `JEB`, `eLife`, `PNAS` (fine-grained — pull from `Shared drives/Blackmon Lab/Projects/paper-reviewer/references/elife-vocabulary.md` if eLife)

If user names a venue not in this list, treat as `journal_generic` plus
ask "want me to apply any specific format constraints from this journal's
guide-for-authors?"

## Output contract

Heath's manuscripts live in Google Docs. The Google Docs API does NOT
support creating suggestions programmatically — only direct edits and
comments — so the closest equivalent of a Word "tracked changes" review
uses **copy + edit + Compare-documents**:

1. **A new Google Doc named `<original-title>_review_<YYYY-MM-DD>`**,
   placed in the same Drive folder as the manuscript. This is a copy
   of the original with all TEXT_REPLACE / INSERT / DELETE findings
   applied as direct edits, plus margin comments for every COMMENT
   finding.

2. **A sidecar `<dir>/<stem>_pre-submission-summary.md`** — a one-page
   narrative summary of the major issues, severity-ranked, for Heath to
   scan before opening the review copy.

After both files are written, Heath opens the review copy in Google Docs
and uses **Tools → Compare documents** with the original selected as the
"compare with" target. This produces a proper visual diff with change
marks at every edit point — the Google Docs equivalent of Word's tracked
changes. Comments anchor naturally and appear in the margin.

**If the input is a `.docx` file (not a Google Doc):** alternative path
documented in the "Format normalization" phase below — at present, the
chat agent doesn't have a Word-format tracked-changes generator tool
(would require python-docx + lxml OOXML wrangling); the fallback for
`.docx` input is to upload it to Drive as a Google Doc first, then
follow the same copy-edit-compare flow.

## Reference bundle

This skill REUSES the paper-reviewer bundle for agent prompts and
checklists, with stance adjustments documented below. Read on demand:

```
Shared drives/Blackmon Lab/Projects/paper-reviewer/
├── agents/methods-specialist.md      # → Methods Auditor (stance: pre-submission)
├── agents/citation-verifier.md       # → reusable as-is
├── agents/adversarial.md             # → reusable mostly (intensity flip below)
├── agents/synthesizer.md             # → REPLACED by inline synthesis below
├── agents/refiner.md                 # → REPLACED by inline classification rubric below
├── checklists/general-biology.md     # → reusable
├── checklists/statistics.md          # → reusable
├── checklists/phylogenetics.md       # → reusable
├── checklists/comparative-methods.md # → reusable
├── checklists/genomics.md            # → reusable
└── references/output-template.md     # → NOT used (output is docx, not markdown)
```

For Heath's manuscript voice (used in the Voice-match pass), use
`agent/skills/voice-matching/SKILL.md` and the underlying
`agent/voice_index.py` exemplars. **Do NOT use paper-reviewer/voice.md** —
that's how Heath writes peer reviews, not how Heath writes papers.

## Stance adjustment

The same agent prompts from the paper-reviewer bundle are reused with this
preamble prepended at dispatch time:

> *You are an internal lab advisor reviewing Heath's own manuscript before
> he submits it. Your job is to find every issue a hostile reviewer would
> find, so Heath can fix them now. The author of this paper is Heath
> Blackmon (your principal). Be ruthless on the science but constructive
> on framing — every issue you flag should come with a concrete fix the
> author can apply. The "verdict" framing of peer review (accept/revise/
> reject) does NOT apply here; the verdict is pre-submitted to be `submit`
> by definition. Your job is to make `submit` defensible.*

The Adversarial Reader gets an additional preamble:

> *Specifically: you are simulating reviewer 2 from hell. What would the
> most hostile competent reviewer in this subfield attack? Cite the exact
> passage they would attack and the strongest fatal-flaw reading of it,
> even if you think the reading is uncharitable. Heath needs to see those
> attacks NOW, while he can still pre-empt them.*

## Architecture (8-agent pipeline)

```
Phase 0: Format identification + normalization (inline)
  Input shape determines the output workflow:

  - **.gdoc shortcut** (most common — Heath's manuscripts live in
    Google Docs)
      → read content via read_local_file (which dispatches .gdoc to
        the Drive API export-as-text path)
      → output workflow = copy-edit-compare (see Phase 6)
      → record source_doc_id for the copy step

  - **.docx file**
      → extract text via read_local_file
      → ASK Heath whether to:
          (a) upload to Drive as a Google Doc and use the standard
              copy-edit-compare flow, OR
          (b) write a markdown review only (no inline edits — the chat
              agent does not currently have a Word-format tracked-
              changes generator; that is a known gap)
      → output workflow depends on the answer

  - **.pdf file** (e.g. typeset preprint)
      → extract text via read_local_file (uses pypdf)
      → no inline edits possible (the source is read-only typeset PDF)
      → output workflow = markdown review file only
      → tell Heath this in Phase 7 so he is not surprised

  - **.md / .tex file**
      → read content directly
      → output workflow = markdown review file only

  Save analysis state to <dir>/.pre-submission-review/state.json
  including {input_format, source_doc_id (if Google Doc),
  output_workflow}.

Phase 1: Coordinator (Sonnet, in-line)
  - Read the working manuscript end-to-end.
  - Identify target venue (ASK if not provided).
  - Identify topic/methods family — pick checklists from the bundle.
  - Segment paper: Title/Abstract / Intro / Methods / Results / Discussion
                   / Figures / Refs / Cover letter.
  - State.json under <dir>/.pre-submission-review/state.json with
    paper-stem, venue, topic, checklists, segments.

Phase 2: Specialists (DISPATCH IN PARALLEL via spawn_subagent)
  Each receives: relevant section(s) only, agent prompt + stance preamble,
  matching checklists, venue. Each MUST emit findings in the structured
  JSON format below — not free-form prose.

  ┌─ Methods Auditor (Opus, methods-specialist.md + stance preamble)
  │    Sections: Methods, Results, Supplement
  │    Checklists: statistics + matching topic checklist
  │    Focus: stat assumptions, sample size, blinding, multiple-testing,
  │    R/Python code reproducibility, missing controls, model misuse
  │
  ├─ Logic Checker (Sonnet, skeptic.md + stance preamble)
  │    Sections: Intro → Discussion claim/evidence chain only
  │    Focus: every claim in Abstract / Discussion must trace verbatim
  │    to evidence in Results. Flag any "we show X" not actually shown.
  │
  ├─ Presentation Agent (Sonnet, generous-reader.md repurposed)
  │    Sections: full paper
  │    Focus: prose tightness, sentence rhythm, paragraph cohesion,
  │    figure clarity, table formatting. Suggest verbatim text replacements
  │    for awkward sentences (these will become tracked changes if the
  │    Voice-match pass approves).
  │
  ├─ Journal Fit Agent (Sonnet, no bundle prompt — write inline)
  │    Sections: Title/Abstract + Intro + format compliance
  │    Focus: word/page limits, structured-abstract requirements, scope
  │    fit, references format. Use venue-specific guide-for-authors.
  │    For Nature-tier: structured abstract (Background / Methods /
  │    Findings / Interpretation), <200 words. For Am Nat: long-form
  │    abstract OK. Etc.
  │
  ├─ Adversarial Reader (Opus, adversarial.md + reviewer-2-preamble)
  │    Sections: full paper
  │    Focus: WHAT WILL REVIEWERS ATTACK? Every flagged issue must be
  │    framed as "a hostile competent reviewer would say…"
  │    THIS IS THE MOST IMPORTANT AGENT — its job is to pre-empt the
  │    actual reviews Heath will receive.
  │
  └─ Citation Verifier (Sonnet, citation-verifier.md, reusable as-is)
       Sections: every cited paper / every direct quote
       Focus: every reference exists, every quote is verbatim,
       publication years correct, journal abbreviations consistent.
       Use fetch_paper_full_text or pdfgetter to grab cited PDFs.

  Each subagent stashes its structured-JSON output at
  <dir>/.pre-submission-review/<role>.json. Format below.

Phase 3: Synthesizer (Opus, inline — no bundle prompt)
  - Read all six specialist JSON outputs.
  - Deduplicate findings (multiple agents flagged the same issue → keep
    the one with the most precise location_quote and clearest fix).
  - Group by section.
  - Severity-rank: BLOCKING (paper can't be submitted as-is) >
                   SHOULD_FIX (will be flagged in review) >
                   POLISH (minor improvement)
  - Output: <dir>/.pre-submission-review/synthesized.json — list of
    deduplicated, ranked findings.

Phase 4: Refiner (Opus, inline — applies classification rubric below)
  - Classify each finding's fix_type — see "Classification rubric"
    below. Demote anything ambiguous to COMMENT.
  - **NO CAPS — preserve every substantive finding.** A docx with 80
    tracked changes and 50 comments is fine; Heath would rather see
    everything than have the agent silently drop issues it judged
    "weak." The only legitimate reason a finding does NOT land in the
    output docx is true duplication (same issue, same anchor — merged
    by the synthesizer).
  - Verify each tracked change has unique anchoring (the verbatim
    old_text appears exactly once in the manuscript). If old_text is
    ambiguous, demote to COMMENT (preserves the finding; just changes
    the format).
  - Severity-rank every finding (BLOCKING / SHOULD_FIX / POLISH) so
    Heath can triage in Word and so the summary.md can lead with the
    most important ones.

Phase 5: Voice-match pass (Sonnet — uses the voice-matching skill)
  - For each TEXT_REPLACE / INSERT finding with new_text in Heath's prose:
    - Read agent/skills/voice-matching/SKILL.md
    - Use voice_index.py exemplars to verify new_text reads as Heath
    - If it doesn't, either rewrite OR demote to COMMENT with a hint
  - Do NOT touch agent prose in COMMENT bodies (those don't go in the
    final paper; they go in margin balloons that Heath will read and
    rewrite if needed).

Phase 6: Generate output — branches on Phase 0's output_workflow

  ─────────────────────────────────────────────────────────────────────
  WORKFLOW A: visible review markup on a copy (Google Doc input — most common)
  ─────────────────────────────────────────────────────────────────────

  Goal: produce a review-copy of the manuscript where every proposed
  change is visible inline in red — old text marked red+strikethrough,
  new text appended in red right after, both kept readable side-by-side.
  This is the closest visible equivalent to Word tracked changes that
  the Google Docs API supports (the API does NOT allow programmatic
  creation of suggestion-mode markups; that's a UI-only feature).

  1. **Copy the manuscript.** Call:
       copy_google_doc(
         source_doc_id=<source-id>,
         new_title="<original-title>_review_<YYYY-MM-DD>",
       )
     Place in the same Drive folder as the source. Save the returned
     new doc ID in state.json.

  2. **Apply visible markup.** Build a JSON array of changes from the
     refiner's TEXT_REPLACE / INSERT / DELETE findings, then ONE call:
       mark_changes_in_google_doc(
         doc_id=<new-id>,
         changes_json=<JSON list>,
         color="red",
       )
     The tool is best-effort — if any individual change fails (e.g.
     old_text not found verbatim because of subtle formatting), it
     reports which ones failed in its return string. **For every
     failed change, fall through to step 3** (add a comment with
     the proposed edit in the comment body) — Heath explicitly
     prefers "comment for everything that needs to change" as the
     fallback over silently dropping changes.

     Format the changes_json from refiner findings:
       TEXT_REPLACE → {"type": "replace", "old": …, "new": …}
       INSERT       → {"type": "insert",  "after": <location_quote>, "new": …}
       DELETE       → {"type": "delete",  "old": …}

  3. **Add comments — for every COMMENT finding AND for every failed
     markup change from step 2.** Call insert_comment_in_google_doc
     once per item:
       insert_comment_in_google_doc(
         doc_id=<new-id>,
         anchor_text=<location_quote>,
         comment="<severity-prefix>: <body>"
       )
     Severity-prefix: "**[BLOCKING]**" / "[Should fix]" / "[Polish]".
     For changes that fell through from step 2 (markup failed),
     prefix with "[Edit suggestion — could not apply inline]" and
     include OLD: ... NEW: ... in the comment body so the student
     can apply manually.

  4. **Write the sidecar summary** to
     <dir>/<stem>_pre-submission-summary.md with finding counts header
     (BLOCKING / SHOULD_FIX / POLISH totals) and a narrative of the
     top issues. If BLOCKING count is double-digit, lead with a
     one-sentence "this paper has substantial structural issues —
     recommend addressing before circulating to co-authors" headline.

  5. **Tell Heath what landed.** Return the review-copy URL plus a
     summary line like:
       "Applied 47 inline markup edits + 18 comments. 3 markup edits
        failed (anchor not found verbatim) — those are now comments
        instead. Open the review copy at <url>; all changes are
        visible in red, comments are in the margin."

     Note: Tools → Compare documents is NOT needed with this workflow
     — the visible-markup approach already makes every change obvious
     in the doc itself. (Compare-documents is the fallback if for
     some reason the markup tool wasn't usable.)

  ─────────────────────────────────────────────────────────────────────
  WORKFLOW A-FALLBACK: comments-only (if mark_changes_in_google_doc is
  unavailable for any reason)
  ─────────────────────────────────────────────────────────────────────

  Heath explicitly preferred "no edits, only comments for every single
  thing that needs to change" over silently-dropped changes. So if
  step 2 above is unavailable (tool errors, permission issues, etc.):

  1. Copy the manuscript via copy_google_doc (same as above)
  2. Skip step 2 entirely
  3. For EVERY refiner finding (TEXT_REPLACE / INSERT / DELETE / COMMENT):
     insert_comment_in_google_doc with the proposed change in the body.
     For TEXT_REPLACE: include OLD: ... NEW: ... so the student can
     see exactly what to change.
  4. Write sidecar summary
  5. Return URL with a note that this is the comments-only fallback.

  ─────────────────────────────────────────────────────────────────────
  WORKFLOW B: markdown review only (.pdf / .md / .tex input, or
  .docx where Heath chose option (b))
  ─────────────────────────────────────────────────────────────────────

  Write a single markdown file `<dir>/<stem>_pre-submission-review.md`
  organized by severity, then by section. For each finding:

      ## [BLOCKING] Methods §2.3 — sample size
      **Anchor:** "across 61 species in our dataset"

      **Issue:** Power analysis is missing for the BiSSE comparison.

      **Suggested fix (TEXT_REPLACE):**
      OLD: across 61 species in our dataset
      NEW: across 61 species (post-hoc power = 0.78 for D=0.10) in
           our dataset

      **Why this matters (reviewer 2):** Reviewer 2 will ask why
      you're doing BiSSE on 61 species without a power analysis…

  Group all TEXT_REPLACE / INSERT / DELETE findings under a "Suggested
  edits" subsection per anchor, and all COMMENT findings under
  "Discussion points." Heath applies the edits manually.

  ─────────────────────────────────────────────────────────────────────

  Update state.json phase=complete with the output paths/URLs.

Phase 7: Tell Heath what happened
  - One-paragraph summary: # of tracked changes, # of comments,
    severity breakdown, top 3 BLOCKING issues by section.
  - Path to the docx + summary.md.
```

## Finding JSON format

Each subagent emits a list of findings in this shape. The synthesizer and
refiner consume these directly.

```json
{
  "id": "methods_auditor_1",                    // unique within the run
  "agent": "methods_auditor",                    // emitter
  "section": "Methods | Results | Discussion | …",
  "location_quote": "verbatim text from manuscript that anchors the issue",
  "severity": "BLOCKING | SHOULD_FIX | POLISH",
  "issue": "what's wrong, in one sentence",
  "rationale": "why this matters; what reviewer 2 will say if not fixed",
  "fix_type": "TEXT_REPLACE | INSERT | DELETE | COMMENT",
  "old_text": "verbatim text to delete (TEXT_REPLACE / DELETE only)",
  "new_text": "replacement / insertion text (TEXT_REPLACE / INSERT only)",
  "comment": "human-readable explanation that goes in the Word margin"
}
```

`location_quote` is always present and verbatim — the docx emitter uses
it to find the anchor in the original document.

## Classification rubric (Refiner applies)

A finding is `TEXT_REPLACE` only if ALL of these hold:

- `old_text` is verbatim from the manuscript (and unique — appears once)
- `new_text` is ≤2 sentences
- `new_text` doesn't materially change the science (it's prose tightening,
  citation fix, factual correction with confident source — NOT a
  reframing of a claim)
- The fix is unambiguous — there isn't a legitimate alternative the
  author would prefer
- The replacement reads as Heath (Voice-match pass confirms)

A finding is `INSERT` only if:
- The location anchor is unambiguous (one specific point in the doc)
- `new_text` is a missing citation, missing definition, missing units,
  missing N=, missing p-value — concrete, low-controversy additions

A finding is `DELETE` only if:
- `old_text` is clearly redundant (repeated sentence, contradiction
  with no judgment call about which to keep, broken cross-reference)

Everything else is `COMMENT`. **When in doubt, COMMENT.** A bad tracked
change is worse than a good comment — Heath has to undo it; a comment is
just visual.

Severity tags carry through to the comment body so Heath can prioritize
in Word: prefix BLOCKING comments with `**[BLOCKING]**`, SHOULD_FIX with
`[Should fix]`, POLISH with `[Polish]`.

## No output caps — preserve everything substantive

Heath has explicitly opted into completeness over docx ergonomics: he'd
rather see 80 tracked changes and 50 comments than have the agent silently
drop findings it judged "weak." Don't drop. The only finding that
legitimately doesn't appear in the output is one that's a true duplicate
of another (same issue, same anchor — merged in the synthesizer's
deduplication step, not "dropped").

Severity tags (BLOCKING / SHOULD_FIX / POLISH) are still attached to every
finding — they're how Heath triages in Word, and how the summary.md
prioritizes its narrative. They are not a basis for dropping.

The summary.md MUST surface counts at the top so Heath knows the volume
before opening the docx:

> **Summary**: 24 BLOCKING, 47 SHOULD_FIX, 31 POLISH findings — 78 tracked
> changes + 24 comments in `<stem>_pre-submission.docx`.

If the BLOCKING count is double-digit, lead the summary narrative with
"this paper has substantial structural issues — recommend addressing these
before circulating to co-authors" (one sentence, then the BLOCKING list).
That's a HEADS-UP, not a drop — the docx still contains every finding.

## Cost / latency

Same as paper-reviewer: ~$1.00–1.50 per review, 5–10 minutes wall clock
with parallel dispatch. The output step (copy + N×replace + M×comment
Drive API calls) adds a few seconds and ~$0 (Drive API is free; no
extra LLM tokens).

## Resumability

Each subagent's JSON output stashed under `<dir>/.pre-submission-review/`.
On resume, check state.json's phase and skip completed stages.
**NEVER re-run Citation Verifier** — its output is expensive and
deterministic; cache it.

## Common errors to avoid

- **Don't suggest prose Heath wouldn't write.** The Voice-match pass is
  there for a reason. If it can't approve a `new_text`, demote to
  COMMENT with a hint, not a tracked change.
- **Don't comment on the Title or Abstract for prose taste.** Those go
  through co-author review separately. Only comment on Title/Abstract
  if there's a factual or claim-evidence problem.
- **Don't repeat the same issue across multiple agents in the output.**
  Synthesizer's deduplication is mandatory — if Methods Auditor and
  Adversarial Reader both flag the same stat issue, merge into one
  finding with the strongest framing (Adversarial Reader's "reviewer 2
  will say…" framing usually wins).
- **Don't generate a tracked-changes docx if the input was a PDF
  scanned image** (no extractable text). Refuse and ask Heath for a
  text-bearing version.
- **Don't tell Heath "this paper is not ready" without naming the 3 most
  consequential BLOCKING issues** — the call-to-action must be
  actionable, not a verdict.

## Distinguishing from related skills/tools

- `paper-reviewer` (skill) — for **someone else's** paper Heath is
  reviewing for a journal. Different stance, different output (markdown
  review for the editor / authors).
- `pre_submission_review` (legacy tool) — single-shot, returns markdown
  with 3 personas. Still available for "quick gut check" without the
  full multi-agent dispatch. This skill SUPERSEDES it for substantive
  pre-submission reviews; the tool stays for the lightweight case.
- `manuscript-polisher` (Anthropic skill) — for prose editing. This
  skill USES voice-matching for prose-level changes but is a
  full-manuscript review, not just polishing.
- `grant-section-drafter` (TEALC skill) — for DRAFTING grant sections.
  Use first; then come back to this skill for pre-submission review.

## When to ask Heath rather than guess

- Target venue not specified → ASK (every recommendation depends on it)
- Manuscript is a co-authored paper → ASK if other authors should see
  the docx first or if Heath wants to apply changes before circulating
- Specific subsections marked "DRAFT — skip" or similar → ASK before
  reviewing those (or skip; obey the marker)
- Paper exceeds the venue's word/page limit → ASK whether to flag as
  BLOCKING or treat as a known issue Heath is aware of
- Heath has reviewed for the journal before with a known stance → ASK
  if any standing guidance applies
