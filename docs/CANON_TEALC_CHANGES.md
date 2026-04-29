# Canon Architecture — Tealc Design Items

**Date:** 2026-04-21
**Context:** 22 of Heath's PDFs moved from `blackmon.pubs/<year-letter>.pdf` to
`/Users/blackmon/Desktop/GitHub/coleoguy.github.io/pdfs/<DOI_SLUG>.pdf`.
A mapping is at `data/pdf_doi_map.json`. A wiki-rename agent is migrating paper pages
from year-letter slugs to DOI slugs. An OA-ingest agent is pulling external papers
under the same DOI-slug naming.

This document covers the Tealc-side items that need Heath's design decisions before
implementation. Mechanical fixes (WIKI_HANDOFF.md wording, docstring cleanup) have
already been applied in this session.

---

## 1. Voice index: PDF source path for Heath's own papers

### What currently happens

`agent/voice_index.py` builds a TF-IDF retrieval index with three sources:

1. **Curated passages** — `data/voice_passages.json` (paragraphs manually curated from
   Heath's Discussion/Methods/Conclusions sections). This is the primary source.
2. **OpenAlex** — abstracts fetched live (fallback when curated count < 40).
3. **Publications JSON** — title-only passages from
   `~/Desktop/GitHub/coleoguy.github.io/data/publications.json` (last resort).

The index currently has no awareness of the PDF files themselves. It does not read PDFs
directly; it works from curated text snippets already in `data/voice_passages.json`.

### What breaks / degrades under Canon

Nothing breaks. The voice index is PDF-path-agnostic — it ingests text, not files.

**Opportunity missed:** Now that full-text PDFs for all 22 Heath papers live at
`pdfs/<DOI_SLUG>.pdf` on the website repo, the curated passages file could be
auto-refreshed by reading those PDFs and extracting Discussion/Methods paragraphs.
Currently the curated passages are hand-populated and may be stale or incomplete.

### Options

**A. No change (status quo).** Voice index continues from `data/voice_passages.json`.
   Heath manually maintains that file. Works fine; voice quality depends on curation effort.

**B. Auto-refresh from Canon PDFs.** Add a step to `rebuild_voice_index` that reads
   each `pdfs/<DOI_SLUG>.pdf` from the website repo, extracts Discussion/Methods
   sections with a lightweight heuristic (find headers, grab paragraphs), and upserts
   into `data/voice_passages.json` — flagging auto-extracted entries separately from
   hand-curated ones so Heath can override.

**C. Hybrid.** Auto-extract (B) but only for DOI slugs not already covered in the
   curated file, preserving hand-curated entries verbatim.

### Heath decisions needed

- Is the curated file adequately maintained, or would auto-refresh from PDFs be
  worth the extraction complexity?
- If B or C: should auto-extracted passages land directly in `voice_passages.json`
  or in a separate sidecar file?

### Rough effort estimate

- A: 0 hours.
- B: ~6–8 hours (PDF text extraction, Discussion-section heuristic, merge logic).
- C: ~8–10 hours (same as B plus merge/precedence logic).

---

## 2. `_pick_paper_slug()` — Heath's own papers when ingested via Drive

### What currently happens

`agent/jobs/wiki_pipeline.py: _pick_paper_slug(meta)` selects a wiki page slug:

1. If DOI is present → `doi_to_slug(doi)` (Canon-correct).
2. If Drive filename only → `title_to_slug(filename)` — e.g. Drive file named
   `"2015a.pdf"` yields slug `"2015a"`, which is a year-letter stub.
3. If no DOI or title → SHA256 fingerprint prefix.

When Heath re-ingests a Drive PDF that has already been renamed to a DOI slug on the
website, the pipeline will write a **new** year-letter-slug paper page while the
website already has the DOI-slug page. That creates a duplicate.

### What breaks / degrades under Canon

- Re-ingesting a Heath paper from Drive (without explicitly passing `source_doi`)
  produces a stub-slug page, not a DOI-slug page.
- `batch_ingest_folder.py` always passes `dry_run=True` and no DOI — it relies on the
  pipeline to infer DOI from the PDF content (the `_resolve_doi_source` path). But
  `run_on_drive_pdf` uses Europe PMC only for the DOI path, not Drive. Drive PDFs
  don't auto-get a DOI unless the caller supplies one.
- wiki_janitor check 2 (`_STUB_RE`) will correctly flag the resulting pages as stubs.

### Options

**A. Require caller to supply DOI for Drive ingests (status quo).** If Heath calls
   `ingest_paper_to_wiki(source_type="drive", source_value=FILE_ID, source_doi=DOI)`,
   everything works. Document this requirement more prominently.

**B. Consult `data/pdf_doi_map.json` in `_resolve_drive_source()`.**
   On Drive ingest, after fetching the Drive filename, check
   `pdf_doi_map.json`'s mapping by `old_name` (e.g. `"2015a.pdf"`) and auto-attach
   the DOI if found. Zero API cost; works for all 22 Heath papers in the mapping.

**C. Attempt CrossRef/Europe PMC DOI lookup from PDF text after extraction.**
   During `_run_pipeline_core`, if `meta["doi"]` is still empty, extract the DOI from
   the PDF's first page (common format: "doi: 10.xxx/...") via regex, and attach it.
   Graceful fallback if no DOI found in text.

**D. Combine B + C.** Map lookup first; regex from text as secondary attempt.

### Heath decisions needed

- Is the `pdf_doi_map.json` stable enough to rely on as a lookup table, or will it
  be modified/replaced as the Canon rename agent runs?
- Should Tealc auto-detect DOIs from PDF text (C), or is that over-engineering when
  the user can always pass `source_doi` explicitly?
- Should `batch_ingest_folder.py` be updated to consult the map by Drive filename?

### Rough effort estimate

- A: 0 hours.
- B: ~2–3 hours (load map, match by old_name in `_resolve_drive_source`).
- C: ~3–4 hours (regex + API fallback, edge-case handling).
- D: ~4–5 hours.

---

## 3. `title_to_slug()` — removes year-letter stubs but not title-derived stubs

### What currently happens

`title_to_slug()` is the fallback slug generator when no DOI is known. It converts the
Drive filename (e.g. `"2022 why not sex chromosomes.pdf"`) to
`"2022_why_not_sex_chromosomes"`. This is better than `"2022a"` but still starts with
a year and will look like a legacy slug to human readers.

### What breaks / degrades under Canon

Title-derived slugs that start with a year (`"2022_why_not_..."`) could confuse the
`_STUB_RE` regex in wiki_janitor (which matches `\d{4}[a-z_ -]{0,25}`). Short
title-derived slugs that happen to match the pattern will be flagged as stubs even
though they have a real title in the frontmatter.

The `_STUB_RE` pattern is applied to the **title** field value, not the filename — so
this is only a problem if a title-less paper page ends up with a title that looks like
`"2022_why_not"`. In practice this would mean `title:` in the frontmatter is
`"2022_why_not"` (a slug used as a title), which is exactly the stub condition.

### Options

**A. No change.** The real fix is ensuring every paper gets a real title — either from
   the PDF header or from the caller passing `title=`. The slug generator doesn't need
   to change.

**B. Suppress year-prefix from `title_to_slug()`.** Strip leading `YYYY` from the
   resulting slug before returning. Reduces confusion but means slug no longer tracks
   Drive filename.

### Heath decisions needed

- Is the current `title_to_slug()` output acceptable? The slug doesn't appear on the
  public website (the permalink is generated from it, but the page title is what
  visitors see).

### Rough effort estimate

- A: 0 hours.
- B: ~30 minutes.

---

## 4. `ingest_paper_to_wiki` tool — no `pdf_url` field in paper pages

### What currently happens

The paper page markdown written by `_compose_paper_page()` includes `doi:`,
`fingerprint_sha256:`, `authors:`, `journal:`, `year:`, `topics:`, `tier:`,
`ingested_at:`, and `permalink:`. It does NOT include a link to the publicly served PDF.

### What breaks / degrades under Canon

Under Canon, Heath's 22 papers now have publicly served PDFs at
`https://coleoguy.github.io/pdfs/<DOI_SLUG>.pdf`. A paper's wiki page could link
directly to the PDF. Currently it does not. This is a missed opportunity, not a break.

External papers ingested via the OA-ingest agent also have DOI-slug PDFs at the same
path if they are open access; Tealc has no way to surface those links either.

### Options

**A. No change.** PDFs are accessible via the DOI link; no explicit PDF link in wiki.

**B. Add `pdf_url:` to paper frontmatter.**
   In `_compose_paper_page()`, if the DOI is known, compute the expected PDF URL:
   `https://coleoguy.github.io/pdfs/<doi_slug>.pdf` and write it as `pdf_url:` in the
   YAML front matter. The Jekyll template could then render a "Download PDF" link.
   Requires: verifying the PDF exists at that URL (or at least that the mapping shows
   it should).

**C. Add `pdf_url:` only when the mapping confirms the PDF exists.**
   Check `data/pdf_doi_map.json` (and the actual file on disk at
   `~/Desktop/GitHub/coleoguy.github.io/pdfs/<slug>.pdf`) before writing the field.

### Heath decisions needed

- Should wiki paper pages link to the canonical PDF?
- Is a `pdf_url:` frontmatter field the right mechanism, or should this be handled
  by a Jekyll template lookup?
- Does the Jekyll theme support a "Download PDF" button if given a `pdf_url:`?

### Rough effort estimate

- A: 0 hours.
- B: ~2 hours (field addition to `_compose_paper_page`, WIKI_HANDOFF update).
- C: ~3 hours (B plus map-lookup verification).

---

## 5. `wiki_janitor.py` — stub detection vs. Canon rename migration

### What currently happens

`wiki_janitor.py` check 2 (`check_stub_titles`) flags paper pages whose `title:` value
matches `_STUB_RE = r'^\d{4}[a-z_ -]{0,25}$'`. This catches pages with title `"2014b"`
(stub) but not pages with a real title.

Separately, check 8 (`check_broken_slug_links`) flags broken `/knowledge/papers/<slug>/`
links in topic bodies.

### What breaks / degrades under Canon

As the wiki-rename agent renames pages from `2012a.md` → `10_xxx.md`, all topic pages
that contain `/knowledge/papers/2012a/#finding-1` links will have broken links until
they are updated. The janitor's check 8 will surface these as `[ERROR]` entries — this
is correct behavior, but the volume will be high during the migration window.

Additionally, if a topic page's `papers_supporting:` list still contains the old DOI
and the paper slug has been renamed, check 7 will incorrectly flag the topic.

### Options

**A. No change; let the janitor surface errors during migration.** Heath or the rename
   agent fixes cross-references as part of the rename pass. The janitor acts as the
   verification step.

**B. Add a `--migration-mode` flag to wiki_janitor** that suppresses check 8 errors
   for slugs that appear in `pdf_doi_map.json` as migrating old names. Only reports
   errors for slugs that have no known migration path.

**C. Add a janitor check 10: orphaned DOI-slug links.** After the Canon rename agent
   runs, there may be old year-letter slugs referenced in topic pages that now have
   DOI-slug equivalents in the mapping. A new check compares broken links against
   `pdf_doi_map.json` and proposes the correct replacement link — turning errors into
   actionable suggestions.

### Heath decisions needed

- Should the janitor's weekly briefing suppress migration-phase errors automatically,
  or should Heath see all errors so he knows where the rename agent is incomplete?
- Is check 10 worth implementing now, or after the migration is complete?

### Rough effort estimate

- A: 0 hours.
- B: ~2–3 hours.
- C: ~3–4 hours.

---

## 6. New tools worth considering

These tools should be reviewed by Heath before any implementation begins. None were
implemented in this session.

---

### 6a. `resolve_canonical_doi(paper_ref: str) -> str`

**What it does:** Given any paper reference — year-letter slug (`"2015a"`), DOI
(`"10.1111/evo.12345"`), or partial title — returns the canonical DOI and DOI slug,
consulting `data/pdf_doi_map.json`, the paper_findings DB, and CrossRef in that order.

**Why it matters:** Tealc currently has three disjoint identifiers for a paper: the
year-letter slug (legacy), the SHA256 fingerprint (internal), and the DOI. Any tool
that takes a paper reference must handle all three. A single resolver collapses them.

**Preconditions:** `data/pdf_doi_map.json` must be treated as authoritative for Heath's
own papers. CrossRef lookup adds latency (~1–2 s) and requires network.

---

### 6b. `ask_canon(question: str) -> str`

**What it does:** Federated search entry point that fans out a natural-language question
across: the lab wiki (topics + papers), the output ledger, the voice index, and the
resource catalog — then synthesizes results into a ranked answer.

**Why it matters:** Currently Tealc must chain `list_wiki_topics` → `read_wiki_topic`
→ `list_output_ledger` manually. `ask_canon` would be the single entry point for
"what does the lab know about X?" queries — reducing prompt complexity and LLM calls.

**Design complexity:** High. Requires a lightweight retrieval ranker across heterogeneous
result sets. Recommend implementing as a Python tool backed by the existing TF-IDF
voice index plus SQLite FTS on paper_findings.

---

### 6c. `get_paper_page(doi: str) -> str`

**What it does:** Given a DOI, reads the corresponding wiki paper page from the website
repo and returns its full markdown — or a helpful error if the page doesn't exist (with
the expected DOI slug so the caller can ingest it).

**Why it matters:** Currently `read_wiki_topic` exists for topic pages but there is no
symmetric tool for paper pages. Tealc must construct the path manually, which is fragile.
`get_paper_page` would encapsulate the `doi_to_slug` → path construction → file read
chain and validate existence in one call.

**Effort:** ~2 hours. Low complexity; high utility.

---

### 6d. `list_canon_pdfs() -> str`

**What it does:** Scans `~/Desktop/GitHub/coleoguy.github.io/pdfs/` and returns a
table of all DOI-slug PDFs present: slug, DOI (reconstructed), file size, and whether
a matching wiki paper page exists.

**Why it matters:** Provides a quick audit of the Canon PDF library vs. the wiki. Lets
Heath or Tealc identify papers that have a PDF but no wiki page (ingestion opportunity)
or a wiki page but no PDF (broken download link).

**Effort:** ~1.5 hours.

---

## Summary table

| Item | Current state | Canon impact | Recommended action | Effort |
|---|---|---|---|---|
| 1. Voice index PDF source | Hand-curated passages | Opportunity: use Canon PDFs | Option A (status quo) unless Heath wants auto-refresh | 0–10 h |
| 2. Drive ingest DOI detection | Caller must supply DOI | Year-letter stub pages on re-ingest | Option B: consult `pdf_doi_map.json` in resolve | 2–3 h |
| 3. `title_to_slug` year prefix | Year-prefixed slugs possible | Minor confusion, not a break | Option A (no change) | 0 h |
| 4. PDF URL in paper pages | Not present | Missed opportunity for PDF links | Option A or B depending on Jekyll support | 0–3 h |
| 5. Janitor during migration | Reports all broken links | High error volume during rename | Option A (let janitor surface errors) or B | 0–3 h |
| 6a. `resolve_canonical_doi` | Not implemented | Would unify paper references | Implement after migration stabilizes | ~4 h |
| 6b. `ask_canon` | Not implemented | Federated wiki + ledger search | Design review needed; high complexity | ~16 h |
| 6c. `get_paper_page` | Not implemented | Symmetric to `read_wiki_topic` | Implement soon; low complexity | ~2 h |
| 6d. `list_canon_pdfs` | Not implemented | Canon PDF library audit | Implement after migration settles | ~1.5 h |
