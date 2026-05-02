---
name: voice-matching
description: >
  Write extended prose (>~150 words) that reads as the researcher's own writing,
  not as AI-generated text. Use when producing grant sections, cover letters,
  addenda, rebuttals, manuscript drafts, lab-website updates, or any output
  that will be read by a program officer, journal reviewer, or hiring committee
  as the researcher's first-person work. The voice index (agent/voice_index.py)
  provides retrievable exemplars of the researcher's published prose ranked by
  topic similarity; this skill describes how to use them and what patterns to
  actively avoid.
---

# Voice Matching

## When to Use

Use this skill for any prose output longer than approximately 150 words that
will be presented as the researcher's own writing. Canonical triggers:

- Grant sections (Specific Aims, Significance, Innovation, Approach,
  Broader Impacts, Data Management Plan narratives)
- Cover letters for manuscript submissions or job-search packets
- Response-to-reviewer letters
- NIH progress reports and administrative supplements
- Lab-website text (research descriptions, people pages, news items)
- Manuscript Discussion or Introduction sections drafted for review

**Do not use for:**
- Chat replies and tool-output summaries (these should be efficient, not
  voice-matched — they go to the researcher, not an external reader)
- Internal notes, database comments, or annotation fields
- Bullet-point action items or todo lists
- Short confirmations ("done", "here is the list you asked for")

The test: will this text be read by someone external to the lab as the
researcher's own words? If yes, use this skill.

---

## The Voice Index

`agent/voice_index.py` maintains a TF-IDF retrieval index of the researcher's
published prose. The primary source is `data/voice_passages.json`, which
contains curated paragraph-level passages from Discussion sections, grant
narrative paragraphs, and lab-website prose. When the curated file has fewer
than 40 passages, the index is supplemented from OpenAlex abstracts for the
researcher's publications (fetched via the OpenAlex API using the configured
`OPENALEX_AUTHOR_ID`).

The index supports two retrieval modes:

**TF-IDF passage retrieval (primary for style matching):**

```python
from agent.voice_index import retrieve_exemplars, voice_system_prompt_addendum

# Get top-k passages ranked by topic similarity
exemplars = retrieve_exemplars(query_text, k=4)

# Get a formatted system-prompt snippet with style anchor text
addendum = voice_system_prompt_addendum(query_text, k=3)
```

Each exemplar returned by `retrieve_exemplars` contains:
- `passage` — a ~350-character snippet of the actual prose
- `title` — paper title or source label
- `year` — publication year
- `similarity` — TF-IDF cosine score
- `register` — optional tag (e.g., "discussion", "grant_narrative", "website")
- `purpose` — optional short description of the passage's rhetorical purpose

**Sentence-embedding retrieval (for claim similarity, not style):**

```python
from agent.voice_index import retrieve_similar_sentences, retrieve_similar_claims

# Top-k sentences by semantic similarity (requires populated npz foundation)
sentences = retrieve_similar_sentences(query, k=12, min_cosine=0.55)

# Top-k subject-predicate claims from the researcher's corpus
claims = retrieve_similar_claims(subject="sex chromosome", k=5)
```

Sentence-embedding retrieval is used by the hypothesis pipeline and contradiction
checker, not primarily by this skill. For voice matching, use `retrieve_exemplars`
or `voice_system_prompt_addendum`.

---

## How to Use the Exemplars

1. **Before drafting**, call `voice_system_prompt_addendum(topic_query, k=3)`.
   The query should describe the topic of the section, not the section label
   (e.g., "karyotype instability beetles comparative analysis" rather than
   "Significance section").

2. **Prepend the addendum to the system prompt** as a style anchor. This is how
   `nightly_grant_drafter.py` uses it:
   ```python
   _drafter_system = _voice_addendum(query, k=3) + "\n\n" + _DRAFTER_SYSTEM
   ```
   If the addendum is an empty string (index not yet built or query returns no
   hits), proceed with the voice principles below and note the gap.

3. **Read the passages before writing.** The exemplars are not templates — do
   not quote them directly. They are calibration references. Before writing the
   first sentence of a section, read the retrieved passages and ask:
   - How many claims does each sentence carry?
   - How is uncertainty hedged — via adverbs, via explicit confidence language,
     or not at all?
   - How often do numbers appear, and at what precision?
   - What is the typical sentence length?

4. **Match those patterns** in the draft. Not as imitation, but as calibration.

---

## Matching Register, Density, Hedging, Quantitative Specificity

The four dimensions that most distinguish the researcher's published prose from
generic academic writing:

**Register.** The prose is direct without being informal. It uses technical
terms precisely (karyotype, dysploidy, pseudoautosomal region, PGLS) without
defining them for a general audience. It does not condescend. It does not
over-explain. When retrieved exemplars consistently use a term without
definition, follow that convention.

**Density.** Sentences carry multiple claims or quantitative constraints.
A single sentence often contains: the subject system, the direction of effect,
the magnitude, the method, and the scope condition. Weak drafts spread these
across several sentences with connective tissue. Match the compression of the
exemplars.

**Hedging level.** The researcher's prose is calibrated but not timid. When
evidence is strong, the prose is declarative. When evidence is preliminary or
the mechanism is inferred, hedging is explicit and specific — not vague
modal softening. Compare:

  Weak: "Our data may suggest a possible relationship between X and Y."
  Matched: "Our preliminary analysis (n = 47 species; Pearson r = 0.38)
  suggests X scales positively with Y, though the pattern may not hold in
  clades with high baseline rates of chromosome number change."

**Quantitative specificity.** Numbers appear early and often. Sample sizes,
effect sizes, p-values, comparison counts, and database record totals are
stated in the prose, not relegated to parenthetical asides or deferred to
figures. If a claim could be made quantitative and the data support it, make
it quantitative.

---

## AI-Assistant Prose Anti-Patterns

The following patterns are diagnostic of AI-generated text and must be actively
eliminated. Their presence signals that voice-matching failed:

**Corporate register:**
- "leveraging our unique dataset"
- "state-of-the-art computational infrastructure"
- "a comprehensive and holistic approach"
- "synergizing across multiple research programs"

**Consulting-deck vocabulary:**
- "robust" as a quality descriptor without specifics
- "novel" without explicit comparison
- "paradigm-shifting", "transformative", "groundbreaking"
- "cutting-edge methods"
- "queryable interface" outside a database-API context

**AI hedging phrases** (the specific form that marks LLM output rather than
scientist output):
- "It is worth noting that..."
- "It is important to emphasize that..."
- "In this context, it is crucial to..."
- "Needless to say..."
- "Taken together, these results suggest..."  (acceptable only as a summary
  sentence if the preceding text actually lists multiple independent lines of
  evidence)

**Validation-forward openers:**
- "In this proposal, we will demonstrate that..."
- "Our approach is uniquely positioned to..."
- "We are ideally suited to carry out this work because..."

**Meta-commentary about the writing:**
- "Here is the drafted section:"
- "I have written the following text for your review:"

When editing a draft produced by another LLM or by an earlier pass: flag every
instance of these patterns by name, and provide a concrete replacement. Coverage
beats pre-filtering — a downstream pass can rank, but missed patterns stay in.

---

## When the Index Is Not Yet Populated

If `voice_system_prompt_addendum` returns an empty string, it means either
`data/voice_index.pkl` does not exist or `data/voice_passages.json` is absent
or too sparse. In this case:

1. Note in the chat response that the voice index was unavailable and style
   calibration is proceeding from first principles only.
2. Apply the voice principles and anti-patterns above as the fallback.
3. If the researcher wants to populate the index, the rebuild job is
   `agent/jobs/rebuild_voice_index.py`; it can be triggered via
   `run_scheduled_job(name="rebuild_voice_index")`.

---

## Scope Boundary

This skill does not replace the grant-section-drafter skill. The two work
together: voice-matching provides the style calibration layer; the
grant-section-drafter skill provides the preliminary-data discipline, the
banned-phrase list, the section-matching rules, and the output format. For
grant sections, invoke both: retrieve voice exemplars first, then apply the
drafter rules on top.

For non-grant prose (cover letters, rebuttal letters, lab-website updates)
where the drafter's preliminary-data rules do not apply, this skill operates
alone.
