# TEALC — Next Steps (grant-driven implementation plan)

**Source briefing:** `Shared drives/Blackmon Lab/grants/google/tealc_implementation_briefing.md` (2026-05-01).
**Canonical install:** `/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/`. GitHub clone is the publishable subset; do not edit there as the source of truth.
**Phase 0:** complete (this file). **Phase 1:** awaiting Heath's authorization.

---

## Blocker before Phase 2

**Gemini and OpenAI API keys are not in `.env`.** Only `ANTHROPIC_API_KEY` is set. Task 4 (cross-vendor critic) cannot start until both are provisioned and the scheduler is restarted to pick up the new env. The other five tasks are unblocked.

---

## Phase 0 findings (2026-05-01)

### (a) CANON_TEALC_CHANGES is not a style guide
The briefing claims `docs/CANON_TEALC_CHANGES.md` governs how changes land. It doesn't — it's a 2026-04-21 backlog of pending Canon/DOI-slug wiki migration design questions. There is **no formal change-discipline doc** in the repo. The actual reference for the modules being touched is `docs/TEALC_V2_HELPERS.md`, which is module-level documentation for the V2 helpers (`cost_tracking`, `model_router`, `ledger`, `critic`, `bundle`, `SCIENTIST_MODE`, `memory_backend`, `project_sessions`, `observability`, `skills/`).

### (b) API-key status
| Vendor | Env var | Status |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | configured |
| Google / Gemini | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | **not set** |
| OpenAI | `OPENAI_API_KEY` | **not set** |

(Google OAuth for Gmail/Drive is via `google_credentials.json` + `google_token.json` — that is not a Gemini API key.)

### (c) TraitTrawler verify_quote.py port source
- **Primary:** `/Users/blackmon/Desktop/GitHub/TraitTrawler/skill/scripts/verify_quote.py` (local clone of `github.com/coleoguy/TraitTrawler`). Version-tracked, easier to diff. **Use this as the port source.**
- Skill bundle copy: `~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/<session-id>/skills/traittrawler/scripts/verify_quote.py` — session ID changes between sessions; do not rely on it.
- Validator design: `pdfplumber` extracts page text → whitespace normalisation → substring match. Has a 50%-prefix fallback for OCR-drift diagnostics. Resolves PDFs via `state/manifest.sqlite (sha256, canonical_path)`. The TEALC port needs an analogous **DOI → PDF resolver** (see Task 1 below).

### (d) Architectural surprises that change the task plan

1. **`output_ledger` schema is single-vendor.** One `critic_score` / `critic_notes` / `critic_model` / `critic_ran_at` triplet per row. Task 4 (Opus + Gemini + GPT-5 in parallel) cannot fit. Cleanest fix: new `critic_scores` table with `(ledger_id FK, vendor, score, notes, ran_at)`. Schema migration the briefing didn't mention.
2. **`agent/critic.py` is Anthropic-hardcoded** (literal `model = "claude-opus-4-7"`, module-level `Anthropic()` client). **`agent/model_router.py` is also Anthropic-only** (constants `SONNET`, `OPUS`, `HAIKU`; no vendor enum). Briefing's "use existing model_router.py abstraction" understates the lift. Task 4 = vendor abstraction + per-vendor client glue + critic refactor + schema migration. **Closer to 2–3 days than the briefing's 1–2.**
3. **Task 3 primitives mostly already exist.** `ledger.update_critic(row_id, ...)` and `ledger.update_user_action(row_id, ...)` already exist with `id`-based linkage. What's actually missing: (i) `critic_pass()` returns a dict but does **not** call `update_critic` itself — every caller is responsible, and likely several aren't doing it; (ii) the agreement-rate query / dashboard panel. **Smaller lift than briefing implies — ~2–3 hours.**
4. **`agent/voice_index.py` has two retrieval layers.** Legacy TF-IDF (`retrieve_exemplars`, `voice_system_prompt_addendum`) AND a Tier-2 sentence-embedding foundation (`retrieve_similar_sentences`, `retrieve_similar_claims` against `data/voice_index_st.npz` + `heath_sentences`/`heath_claims` tables). Task 2's `stripped` mode must gate **both**, plus any direct caller that injects voice prose. Grep for callers before patching, else stripped mode leaks voice through the second pathway.
5. **Eval harness is manual, and is complementary to Task 2.** `evaluations/run_reviewer_circle.py` is a 3-phase manual pipeline (backfill → Gmail-draft invitations → ingest replies + correlations), not an automated benchmark. `evaluations/blind.py` already strips model names, "Tealc", paths, grant codes, emails, PI/lab names — that's **consumer-side blinding** (scrub generated text). Task 2's voice toggle is **producer-side blinding** (don't generate Heath-flavored prose). Both are needed for blinding integrity. The 5 scoring dimensions are Rigor / Novelty / Grounding / Clarity / Feasibility. Only one rubric ships (`rubrics/chromosomal_evolution.md`); the four named benchmark domains will need three more rubric files (Heath's call, out of scope here).
6. **Task 1 needs a DOI → PDF resolver, not just a quote validator port.** TraitTrawler resolves via SHA256 manifest. TEALC has `data/pdf_doi_map.json` for Heath's own papers, but a manuscript can cite anyone's papers. Plan: new `agent/citation_grounding.py:_resolve_pdf(doi)` that consults `pdf_doi_map.json` first → checks `~/Desktop/GitHub/lab-pages/pdfs/<doi_slug>.pdf` → falls through to the `pdfgetter` skill for fetch-on-miss.
7. **Critic ships 6 rubrics, not 4.** Beyond `default, grant_draft, hypothesis, analysis`, there are `wiki_edit` and `repo_note`. `TEALC_V2_HELPERS.md` updated to reflect this.

---

## Phase 1 — parallelizable (4 agents)

Refer to briefing §3 for full task definitions. Items below capture the execution plan with Phase 0 corrections applied.

### Agent A — Task 2: Voice configuration toggle (`aligned` | `stripped`)
- **Effort:** 1–2 hours.
- **Files:** `agent/config.py` (add `voice_mode: aligned | stripped`), `agent/voice_index.py` (gate **both** TF-IDF and sentence-embedding paths), `agent/ledger.py` (record `voice_mode` per output — easiest via `provenance_json` to avoid a schema migration).
- **Pre-work:** grep for every caller of `voice_system_prompt_addendum`, `retrieve_similar_sentences`, `retrieve_similar_claims`, and any other voice-injecting site. Stripped mode is only correct if every site honors it.
- **Acceptance:** same prompt run twice (aligned vs stripped) produces measurably different style; both runs logged in ledger with the correct `voice_mode`.

### Agent B — Task 3: Critic-vs-human disagreement logger
- **Effort:** ~2–3 hours (revised down from briefing's half day; the schema is already there).
- **Files:** `agent/critic.py` (or new wrapper `critic_pass_and_record(row_id, ...)`), audit every existing call-site of `critic_pass()` to ensure `update_critic` runs after, `agent/observability.py` (agreement-rate query), possibly `agent/dashboard_server.py` for a panel.
- **Note:** the existing `update_user_action` already supports `adopted | rejected | ignored`. Use that as the human-decision channel; do not duplicate.
- **Acceptance:** synthetic data injected into the ledger produces a sensible weekly agreement-rate report.

### Agent C — Task 5: Build `heath-manuscript-corpus.md` (first/last author only)
- **Effort:** ~1 day. Entirely outside the TEALC repo — no code conflicts.
- **Output:** two files. Save to `Shared drives/Blackmon Lab/Projects/paper-reviewer/dev/` next to `heath-review-corpus.md`, **or** create new sibling `Shared drives/Blackmon Lab/Projects/manuscript-voice/`. Confirm with Heath before creating a new folder.
- **Filter:** OpenAlex `author_position` field — first or last only. Do NOT use heuristics.
- **Methodology:** mirror `paper-reviewer/voice.md` (Discussion-section signal weighted heaviest). Cross-reference Strunk & White / Pinker / Schimel from `Shared drives/Blackmon Lab/books/writing/`.
- **Acceptance:** voice profile produces measurably-Heath-flavored output when used in `aligned` mode (Task 2). Heath reviews and approves before the profile is checked into TEALC.

### Agent D — Task 6: 20% sampled human review of critic-rejected drafts
- **Effort:** half day.
- **Files:** `agent/critic.py` (emit `critic_reject_sampled_for_review` event with 0.2 probability, configurable), `agent/scheduler.py` (daily/weekly job that flags sampled rejects), `agent/submission_review.py` (reuse `replication_verdict_review` — the renamed 3-persona implementation kept for internal review paths; not the chat-facing `pre_submission_review` tool, which produces marked-up Google Docs and is the wrong shape for this task), `agent/observability.py` (queue-depth dashboard panel).
- **Note:** sampling rate must be configurable; random seed must be reproducible (log it).
- **Acceptance:** synthetic critic rejects produce a queue containing ~20% of inputs (within reasonable variance for small N).

### File-conflict watch (Phase 1)
A, B, and D all touch `agent/critic.py` and `agent/ledger.py`. Cannot run truly in parallel without a merge step. Two options:
- **(i)** one agent owns those files; A/B/D submit patches that the owner applies in one branch
- **(ii)** git worktrees + serial merge after each agent finishes

**Recommendation: option (i)** — simpler. C runs fully parallel from start to finish (different repo).

---

## Phase 2 — after Phase 1 lands (1–2 agents)

### Agent E — Task 1: Citation-grounding gate
- **Effort:** 1–2 days.
- **Files:** new `agent/citation_grounding.py`, hooks in `agent/critic.py` and `agent/bundle.py`, new ledger event in `agent/ledger.py` (likely a column on `output_ledger` or a new `citation_gate_results` table — decide during design), tests in `tests/test_citation_grounding.py` with fpdf2 fixtures.
- **Port source:** `/Users/blackmon/Desktop/GitHub/TraitTrawler/skill/scripts/verify_quote.py`.
- **New work beyond the port:** DOI → PDF resolver (`pdf_doi_map.json` → `~/Desktop/GitHub/lab-pages/pdfs/` → `pdfgetter` fetch-on-miss).
- **Acceptance:** ≥95% true citations pass on five real Heath manuscripts; 100% of injected fakes rejected; manuscripts that fail cannot reach output (route to review queue).

### Agent F — Task 4: Cross-vendor critic toggle
- **Effort:** 2–3 days (revised up from briefing's 1–2).
- **Blocker:** Gemini + OpenAI API keys must be provisioned in `.env`; restart scheduler after.
- **Schema migration:** new `critic_scores` table `(id, ledger_id FK, vendor, model, score, notes, ran_at)`. Cleaner than widening `output_ledger` with per-vendor columns.
- **Vendor abstraction:** add to `agent/model_router.py` (vendor enum, per-vendor client glue), refactor `critic_pass()` to dispatch via the abstraction.
- **Cost:** triples critic API spend during testing. Get explicit budget OK from Heath before running large-corpus tests.
- **Acceptance:** smoke test runs the same input through all three critics; three distinct scores logged with vendor metadata; cross-vendor disagreement-rate report available.

---

## Phase 3 — integration verification (single agent, sequential)

- Run full pytest suite. Resolve any regressions.
- Run TEALC end-to-end on a real (or fixture) draft and verify all six new behaviors fire.
- Build a reproducibility bundle and confirm `tar.gz` + SHA256 manifest still emits cleanly.
- Realistic wall-clock with parallelism: 2–3 working days. Without parallelism: 4–5.

---

## Don'ts (carried over from briefing §5)

- **TEALC is in production daily use.** Do not break daily-flow. Smoke-test before committing.
- **Do not skip pre-commit hooks. Do not amend commits.** Make new commits if a hook fails — never `--no-verify`.
- **Do not push to remote without Heath's explicit OK.**
- **Do not modify TraitTrawler.** Task 1 is a *port* — copy the validator, do not import TraitTrawler as a dependency unless Heath asks.
- **Do not include co-author-only papers in Task 5.** First/last author only via OpenAlex `author_position`.
- **Do not spend Gemini/OpenAI budget on Task 4 without explicit OK.**
- **Do not auto-expand scope.** A 7th task: write it down for Heath, do not implement it.

---

## Recommended kickoff (when Heath authorizes)

1. **Provision Gemini + OpenAI API keys** to unblock Task 4 (low priority; Phase 2). Heath does this manually.
2. **Confirm Task 5 output folder** with Heath (`paper-reviewer/dev/` vs new `manuscript-voice/`). C is otherwise self-contained.
3. **Dispatch Phase 1 with the file-conflict-watch convention applied** — option (i) above.
4. **After Phase 1 lands and tests are green, proceed to Phase 2.**

When work resumes in a future session: read this file first, then `tealc_implementation_briefing.md` for canonical task definitions.

---

## Fix backlog (independent of the six-task plan)

### F1 — DB-path audit: modules that ignore `TEALC_DB_PATH` ✅ COMPLETE 2026-05-01

**Status:** all known DB-bifurcation sites are patched. Live Chainlit + scheduler need to be restarted (or wait for KeepAlive cycle) to pick up the fixes.

**Patched files:**
- `agent/activity_report.py` — one-line `from agent.scheduler import DB_PATH`
- `agent/tools.py` — module-level `DB_PATH` replaced with lazy `_db_path()` accessor; 51 `sqlite3.connect()` sites swept (incl. the `_sql.connect(DB_PATH)` aliased site at line 6264). Four `except ImportError → DB_PATH = str(Path(__file__)...)` fallback blocks at ~7138/7162/7185/7202 are now dead code; harmless but worth cleaning up (low-priority).
- `agent/documents_index.py` — `_DB_PATH` now imports from scheduler
- `agent/jobs/sync_goals_sheet.py`
- `agent/jobs/publish_jobs_doc.py`
- `agent/jobs/mine_project_leads.py`
- `agent/scripts/enrich_projects_from_drive.py`
- `scripts/migrate_grants.py`

**Verified:** zero hand-rolled `agent.db` paths remain across `agent/`, `agent/jobs/`, `agent/scripts/`, `scripts/`. Confirmed via grep + AppSupport DB has 84 research_projects rows (live), Drive DB has 0 (correctly abandoned).

**Why this mattered:** the May 1 self-audit saw "phantom p_002 / p_003 projects" because the chat agent (running in Chainlit) was reading from the abandoned Drive DB through the buggy `tools.py:37` path. The projects existed all along in AppSupport (84 of them, including p_002 and p_003). After Chainlit restarts, the audit's framing of issues 1, 2, 7, 8, 9 dissolves. Items 3, 4, 6 are real residuals — see F4–F6 below.

---

### F2 — Drive backup of all local-only state ✅ INFRASTRUCTURE COMPLETE 2026-05-01 (cutover pending)

**Status:** `deployment/` folder created in Drive with all infrastructure to back up everything currently AppSupport-only. Cutover script written and tested for syntax; **not yet executed** — user runs when ready (chat must be stopped during cutover).

**What was created:**
- `deployment/bin/scheduler-wrapper.sh` — canonical copy of the live wrapper
- `deployment/bin/backup-db.sh` — extended: writes primary backup to AppSupport AND mirrors to `deployment/snapshots/backups/` (60-day retention in Drive); failure to mirror is a WARN, never a hard fail
- `deployment/bin/memories-sync.sh` — new: rsyncs `~/Library/Application Support/tealc/memories/` to `deployment/snapshots/memories/` every 30 min
- `deployment/bin/cutover.sh` — one-shot script that stops live agents, copies deployment/* → AppSupport + LaunchAgents, reloads, smoke-tests
- `deployment/launchd/{scheduler,backup,memories-sync}.plist` — LaunchAgent definitions
- `deployment/apps/{Start,Stop} Tealc.app/` — copies of the bash-wrapped desktop bundles
- `deployment/restore_tealc.sh` — new-Mac one-shot: pip install, mkdir AppSupport tree, restore latest DB snapshot, restore memories, copy plists/apps, launchctl bootstrap. Manual prereqs (Drive sign-in, FDA grant) documented in `deployment/README.md`.

**Cutover steps for current Mac** (when user is ready):
1. Open `Stop Tealc.app`
2. `bash "/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent/deployment/bin/cutover.sh"`
3. Verify with `launchctl print` and a glance at `deployment/snapshots/backups/`
4. Open `Start Tealc.app`

**New-Mac restore time:** ~40 min total — ~30 min Drive sync, ~5 min restore script, ~5 min manual (FDA, OAuth-if-needed). See `deployment/README.md` for full prerequisites.

---

### F3 — Cross-vendor cost-tracking + LLM shim ✅ COMPLETE 2026-05-02

**Status:** vendor-portable LLM shim landed. New module `agent/llm.py` (706 lines) is the single chokepoint for all model calls; `agent/critic.py` ported through it; `agent/cost_tracking.py` now accepts the canonical `Usage` dataclass and computes vendor-aware pricing. End-to-end smoke-tested against Anthropic (Opus 4.7 critic via `model_router.choose_model("critic_pass")` returns score 1, identifies hype flags correctly, costs $0.000165 per call computed from canonical pricing table).

**What landed:**
- `agent/llm.py` — `chat(model, system, messages, tools, max_tokens, cache_hint, effort)` returning a canonical `Response` with normalized `Usage`. Vendor-detected via prefix (`claude-*` → Anthropic native SDK, `gpt-*`/`o1`/`o3`/`o4` → OpenAI via LiteLLM, `gemini-*` → Gemini via LiteLLM). Tool format and message-shape translation across vendors handled by helper functions `_anthropic_to_openai_messages` and `_anthropic_to_openai_tools`.
- `agent/critic.py` — refactored to use `chat()`. Removed direct `Anthropic()` client. Model selection via `model_router.choose_model("critic_pass", log=False)`. Public function signature unchanged for backward compat.
- `agent/cost_tracking.py` — `_normalize_usage()` accepts both `Usage` dataclass and legacy Anthropic-shaped dict. `_compute_cost()` looks up vendor-aware pricing in `_PRICING_USD_PER_M_TOKENS` (13 models across 3 vendors). Schema unchanged.
- `requirements.txt` — added `litellm>=1.83.0`.

**Bugs found and fixed during integration smoke test:**
- Anthropic API rejects `max_tokens <= thinking.budget_tokens`. `_call_anthropic` now silently demotes thinking when caller's `max_tokens` is too small to fit the effort tier's budget.
- The original `raise type(exc)(f"...")` error-wrap crashed because `APIStatusError` requires kwargs that wrappers can't provide. Replaced with `raise RuntimeError(...) from exc`.
- Naming inconsistency in `model_router.py`: `_OPUS_TASKS` uses key `"critic_pass"`, `EFFORT_TIERS` uses key `"opus_critic"`. Critic now passes `"critic_pass"` to route to Opus; effort defaults to "medium". Cleanup ticket: unify model_router task naming (low-priority).

**Path forward to enable users to swap primary agent:**
- Today: critic + any future caller of `agent.llm.chat()` is vendor-portable.
- Next steps to fully enable `TEALC_PRIMARY_MODEL=gpt-5` in `.env`:
  1. Provision Gemini + OpenAI API keys in `.env` (still blocked — same as Phase 0 finding).
  2. Migrate `agent/jobs/*.py` and `agent/graph.py` from direct `client.messages.create` calls to `chat()`. ~35 sites to sweep; can be done one job-family at a time.
  3. Add a `TEALC_PRIMARY_MODEL` env var honored by `model_router.choose_model()` for `chat_default` task type.
  4. Test the 3 non-Anthropic backends with real keys (smoke tests written; not yet run for OpenAI/Gemini due to no keys).

**Caveats:**
- LiteLLM 1.83.14 is now in the venv. It downgraded `jsonschema` 4.26→4.23 and `typer` 0.25→0.23 to satisfy its constraints, and brought in `openai` 2.24 transitively. No TEALC functionality regression observed in smoke tests, but worth keeping an eye on jsonschema-using paths.
- Gemini's `cachedContent` is not implemented in v1 of the shim (TODO in `_call_gemini`). OpenAI prompt caching is implicit (no per-call control).
- `agent/llm.py` does `import litellm` at module load — running scheduler/Chainlit will fail to import if litellm is uninstalled. Already installed in `~/.lab-agent-venv`; documented in `requirements.txt`.

---

### F4 — `tracked` decorator: error-write fragility (audit issue 3, real)

**Where:** `agent/jobs/__init__.py:88-99`. The `except` block does its own `conn.execute(...)` to write the error, then `raise` at line 99. If that `conn.execute` itself throws (WAL lock contention is the most plausible cause), the original exception is suppressed and the re-raise never fires; `job_runs.status` could stay `'running'` forever and the dashboard shows a perpetually in-flight job.

**Fix:** move the error-write into the `finally` block; use a separate connection.

**Effort:** ~1 hour incl. test.

---

### F5 — Silent error swallowing across artifact-grade jobs (audit issue 5, real)

**Pattern:** many jobs use `try → return [] on exception → print() → continue`. The `tracked` decorator records `status='success'` and the dashboard goes green while every underlying query failed. Confirmed in `weekly_review.py`, `nightly_literature_synthesis.py:297,309,395,398`, `weekly_comparative_analysis.py:132,153,230`, `nightly_grant_drafter.py:173,205`. `summarize_sessions` skips silently when checkpoints table is missing — the "no checkpoints table yet" message goes to stdout and never to `job_runs.error`.

**Fix:** module-level logger per job; route caught-but-not-re-raised exceptions through it; surface to `job_runs.error` even when status is `success`. Plus a daily scan that promotes `output_summary` strings like `"extraction failed"` / `"error:"` into briefings.

**Effort:** ~half day across the affected jobs.

---

### F6 — Empty wiki-page publish (audit issue 6, real)

**Where:** `wiki_surface` job. Wrote a blank page because `hypothesis_proposals` returned zero rows.

**Fix:** add an empty-input guard — if the source table is empty, log + skip the publish, don't write a noop page to the live site.

**Effort:** ~30 min.

---

### F7 — 0% prompt-cache hit rate (audit issue 4, real)

**Why:** sub-agent system prompts have no `cache_control` (Agent 4's harness review flagged this as "5-line fix"). Background jobs build full system prompts per call without prompt-caching breakpoints. $7.83 over 14 days is fine in absolute terms; the issue is wasted spend that scales with usage.

**Fix:** add `cache_control: ephemeral` on the static portion of every job's system prompt (where amenable to caching) and on `subagents.py:241`.

**Effort:** ~1 hour for the sub-agent fix; ~half day for the systematic job sweep.

---

### F8 — Drop `--min-score 3` from `evaluations/export_batch.py` (benchmark integrity)

**Why:** the AI-vs-PhD benchmark in `evaluations/export_batch.py` accepts `--min-score 3`, which filters TEALC output by its OWN critic score before sending it to blinded human reviewers. Comparing PhD output to TEALC's post-gating output is a stacked deck — peer reviewers will catch this.

**Fix:** export unfiltered output by default; let human reviewers filter post-hoc.

**Effort:** 30 min.

---

### F9 — Schema-version dispatch (replace ad-hoc ALTER-TABLE-IF-NOT-EXISTS)

**Where:** `agent/scheduler.py` `_migrate()` is now ~1,200 lines of bare `ALTER TABLE ... ADD COLUMN` wrapped in `try/except: pass`. No version table.

**Fix:** add a `schema_version` int in a `_meta` table; convert `_migrate()` to a dispatch table (`if current_version < N: apply_migration_N()`). Migrations run once per install, in order, idempotent.

**Effort:** ~1 day. Future column rename / type change becomes painful without this.

---

### Architectural recommendations from the four-agent review (longer-term)

These came out of the 2026-05-01 multi-agent review. Track separately from F1–F9; they're not bug fixes, they're architecture choices to schedule.

- **Vendor abstraction (`agent/llm.py` shim + `critic_runs` table)** — 2 days. Unblocks Task 4 cleanly. See vendor-coupling agent's report for the seam analysis.
- **Inner experiment loop for `weekly_comparative_analysis`** — 2–3 days. Closes the largest capability gap with frontier (Robin / Co-Scientist). The deterministic sign-propagation in `tier05_sign_propagation` already gives a falsification signal — extending it post-execution is the highest-leverage capability change.
- **System-prompt → `agent/skills/` migration** — 1 day. Finish what `TEALC_V2_HELPERS.md:281` started; ~1,500 token-per-turn savings.
- **Drop LangGraph, collapse chat-agent and sub-agent to one Anthropic-SDK loop, promote tool families to per-skill tool lists** — 1 week. Bold redesign per Agent 4's harness review. ~80% reduction in per-turn input tokens. Defensible against current Anthropic best practice (`Building effective agents`, Agent Skills, Effective harnesses).
