---
name: grant-section-drafter
description: >
  Draft one section of a grant application, manuscript, cover letter, addendum,
  or any extended prose that must read in the researcher's own voice. Use when
  Heath asks for a first-pass draft of a Significance section, a Specific Aim,
  an Approach narrative, a rebuttal, a cover letter paragraph, or any piece of
  scientific prose longer than ~200 words that will be reviewed line-by-line or
  submitted to a funding agency, journal, or search committee. Do NOT use for
  short chat replies, tool-output summaries, or bullet-point notes.
---

# Grant Section Drafter

## When to Use

Trigger this skill whenever the request involves drafting extended prose that
carries institutional weight — grant sections, manuscript sections, cover letters,
NIH-style Specific Aims, addenda, progress reports, or response-to-reviewer text.
The test: would a program officer, journal editor, or search-committee member
eventually read this? If yes, use this skill. If the output is a chat reply or an
internal summary, do not use it.

The skill is also activated automatically by the `nightly_grant_drafter` job
(`agent/jobs/nightly_grant_drafter.py`), which reads the researcher's linked
artifacts overnight, identifies the next unfinished section via a gap-finder call,
and invokes this skill's principles to draft that section.

---

## Voice Principles

All prose produced under this skill must match the researcher's published writing
register. The governing rules are drawn directly from `_DRAFTER_SYSTEM` in
`agent/jobs/nightly_grant_drafter.py` and the `SCIENTIST_MODE` constant in
`agent/jobs/__init__.py`:

**Direct, quantitative, concrete.** Claims need numbers, not adjectives. Instead of
"we have extensive experience with karyotype analysis," write "we have assembled
and curated eight open karyotype databases totaling 85,000+ records across six
major taxonomic groups." Vague abstractions are rejected at draft time, not during
review.

**No corporate register.** The prose must not sound like a consulting deck or a
software company's marketing copy. The reader is a scientist. Write like one.

**Calibrated assertion.** The researcher's published writing distinguishes hypothesis
from finding, correlation from causation, single-study result from replicated
pattern. Drafts must mirror that calibration. Never overstate what the preliminary
data show. If the effect size is uncertain, say so plainly rather than smoothing
it away.

**Terse.** Skip validation-forward openers ("In this proposal we will demonstrate
that..."), meta-commentary, and padding. Get to the claim.

---

## Banned Phrases

The following words and phrases are prohibited in any output produced by this
skill. Their presence signals AI-assistant cadence or corporate register — both
fatal to grant prose:

- comprehensive (as a modifier: "comprehensive framework", "comprehensive analysis")
- robust (as a vague quality claim: "a robust approach", "robust methods")
- holistic
- leveraging (as a verb applied to anything non-mechanical)
- cutting-edge
- queryable (outside a database-schema context)
- paradigm-shifting
- novel — permitted only after explicit comparison to existing work that makes
  the novelty specific ("novel relative to Smith et al. 2019, which did not account
  for...")
- revolutionary, groundbreaking, transformative (any superlative that is not a
  direct quote from a reviewer)
- "In this proposal we will..."
- "a comprehensive framework"
- "state-of-the-art"

When reviewing a draft for these phrases: flag every instance with a concrete
alternative, as required by the `<review_default>` stance. Do not silently delete
them — name the phrase and the replacement.

---

## Preliminary-Data Discipline

Every claim about preliminary results must be grounded in three components:

1. **n** — the sample size or number of observations behind the claim
2. **test** — the statistical or analytical method applied
3. **effect** — the direction and magnitude of the result

Format: "We find [direction + effect] (n = [N]; [test])."

Example: "Karyotype instability correlates positively with genome size across
Coleoptera (n = 847 species; Pearson r = 0.41, p < 0.001, PGLS controlling for
phylogeny)."

When one of these three components is absent from the notes or context available:
do not invent it. Write the placeholder exactly as:

```
[Heath: confirm n / test / effect]
```

This placeholder is intentional: it flags real gaps for the researcher's morning
review rather than propagating confident-sounding fabrications into a grant draft.
The `nightly_grant_drafter` job uses this convention and the researcher's review
workflow (`review_overnight_draft`) is designed around it.

---

## Limitations-Acknowledgment as Default

Strong grant prose acknowledges the scope conditions on every major claim. This
is not weakness — reviewers at NIH, NSF, and top journals reward intellectual
honesty more than bravado. Per the drafter system prompt:

> "Acknowledge limitations explicitly when you make a strong claim — what would
> falsify it, what scope is conditional."

Operationally: after every bold positive claim, add one qualifying sentence that
names the boundary condition or the observation that would falsify the claim.
Omit this only when the claim is already narrow enough to be self-limiting.

---

## Section Matching

The `nightly_grant_drafter` job passes a `section_label` and a
`context_already_present` block from the gap-finder call. This skill must
match both:

- **Section label** — the draft's heading and scope must match the section being
  filled (e.g., "Significance", "Approach Aim 2", "Broader Impacts"). Do not
  expand or contract scope.
- **Context already present** — the surrounding sections already contain claims,
  citations, and structure. The draft must not repeat or contradict what is
  already written. Read `context_already_present` before drafting a single
  sentence.

In chat mode (not the nightly job), the researcher will usually supply the section
context verbally. If he does not, ask for one sentence: "What section is this and
what's already written around it?"

---

## Voice Exemplars

Before drafting, retrieve voice exemplars from the TF-IDF index:

```python
from agent.voice_index import retrieve_exemplars
exemplars = retrieve_exemplars(query=section_label + " " + what_is_missing, k=4)
```

Or call the convenience wrapper used by the drafter job:

```python
from agent.voice_index import voice_system_prompt_addendum
addendum = voice_system_prompt_addendum(query, k=3)
```

The addendum is prepended to the system prompt as a style anchor. It surfaces
passages from the researcher's published prose (Discussion sections, grant
narratives, lab-website text) ranked by TF-IDF cosine similarity to the draft
topic. Match their:

- **Density** — how many claims per sentence
- **Hedging level** — how explicitly uncertainty is flagged
- **Quantitative specificity** — how often numbers appear in support of claims

If `voice_system_prompt_addendum` returns an empty string (index not yet
populated), proceed with the voice principles above and flag that the index
was unavailable.

---

## Output Format

- Output is **markdown only** with no preamble. The first line of the output
  is the section content, not a meta-comment about it.
- No "Here is the draft:" opener. No "I've drafted the following section:"
  closer.
- Headings, if appropriate to the section type, use `##` or `###`.
- Citation placeholders use `(Author et al., YEAR)` format until real DOIs are
  confirmed.
- The `citation_suggester` module in `agent/citation_suggester.py` is invoked
  after drafting by the nightly job; in chat mode, suggest 1-3 specific citations
  inline using your knowledge, flagged as `[suggest citing: ...]`.

---

## Integration with the Nightly Job

The full automated flow in `agent/jobs/nightly_grant_drafter.py`:

1. For the active project with the soonest deadline, read its linked Google Doc
   artifact via `read_drive_file`.
2. Send the artifact to a gap-finder Sonnet call that returns `section_label`,
   `what_is_missing`, and `context_already_present`.
3. Retrieve voice exemplars for the topic.
4. Draft the section using this skill's rules (the `_DRAFTER_SYSTEM` prompt
   encodes them verbatim).
5. Run a critic pass (`agent/critic.py`, `rubric_name="grant_draft"`).
6. Create a new Google Doc tagged `[draft]` — never overwrite the source artifact.
7. Insert a row in `overnight_drafts` and a briefing of urgency `warn` or
   `critical` (based on critic score) so the draft surfaces in the morning.

The `_DRAFTER_SYSTEM` constant (lines 62-78 of `nightly_grant_drafter.py`) is the
canonical machine-readable form of this skill; this SKILL.md is the human-readable
companion.

---

## Anti-Patterns Checklist

Before finalizing any draft, check each item:

- [ ] Contains no banned phrases (see list above)
- [ ] Every preliminary-data claim has n / test / effect, or a `[Heath: confirm ...]` placeholder
- [ ] At least one limitation or boundary condition named per major claim
- [ ] No validation-forward opener ("In this proposal we will...")
- [ ] Register matches voice exemplars (density, hedging, quantitative specificity)
- [ ] Section scope matches `section_label` — not broader, not narrower
- [ ] No fabricated citations, file paths, or numerical results
- [ ] Output is raw markdown with no preamble
