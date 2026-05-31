# Tealc — engineering context

You are working on **Tealc**, an autonomous lab agent for the Blackmon Lab at TAMU. The principal user is Heath Blackmon — a scientist, not a software engineer. Treat him like a smart collaborator who pushes back hard when you're wrong, but who will not necessarily catch architectural mistakes the way an SWE would. Your job is to be the SWE. Push back when his proposed change has a non-obvious downside; don't silently agree and ship.

This file is the operational ground-truth for working on Tealc. Read it on every session.

---

## 1. Where things live (READ THIS FIRST)

There are TWO copies of Tealc on this machine. They are not equivalent.

| Path | Role | Truth status |
|---|---|---|
| `/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive/00-Lab-Agent` | Canonical, running install. `.env`, OAuth tokens, OS launchd agents, scheduler, dashboard all point here. | Source of truth. |
| `/Users/blackmon/Desktop/GitHub/tealc` (remote: `github.com/coleoguy/tealc`) | Publishable subset — the non-private parts mirrored for community visibility. | Read-mostly. NOT a runtime location. |

**Rules:**
- Edits that affect the live chat (system prompt, tools, jobs, CSS, JS, chainlit.md) go to the **Drive** copy.
- Mirror to GitHub only if the change is in the publishable subset. Ask if unsure — most personalized prompt content (Heath profile, lab roster, students, grants) does NOT belong on GitHub.
- **Never propose pointing the local install at GitHub.** GitHub is a publish target.

**Active database location:** `~/Library/Application Support/tealc/agent.db` — NOT in Drive. Drive sync zeroed `data/agent.db` mid-write twice (Apr 24 and Apr 29 2026). The DB now lives outside Drive permanently. `agent/scheduler.py` resolves `DB_PATH` from the `TEALC_DB_PATH` env var, defaulting to that location. **Drive's `data/agent.db` is a stale snapshot — never trust it.**

**Memory storage:** `~/Library/Application Support/tealc/memories/` (`agent/memory_backend.py`, `agent/project_sessions.py`). Same rationale — keep mutable state off Drive sync.

**Backups:** Daily online `.backup` to `~/Library/Application Support/tealc/backups/agent_YYYY-MM-DD.db` via `com.blackmon.tealc-backup` LaunchAgent. Local-only by design.

---

## 2. Architecture in one screen

Four loosely-coupled tiers, all sharing `agent.db`:

1. **Chat tier** — `app.py` (Chainlit on `:8000`) + `agent/graph.py` (LangGraph react agent, ~1k-line system prompt, model routing). Default model **Sonnet 4.6**; "think hard" / "use opus" / "deep thinking" switches to **Opus 4.7**.
2. **Scheduler tier** — `agent/scheduler.py` (APScheduler async, runs as separate process via `scripts/start_scheduler.sh` → launchd `com.blackmon.tealc-scheduler`). Registers ~77 jobs in `agent/jobs/`.
3. **Dashboard tier** — `agent/dashboard_server.py` (FastAPI on `:8001`, local only). Three tabs: tasks, activity, abilities. Fed by `publish_dashboard.py` (every minute).
4. **Evaluation tier** — `evaluations/` (blinded external-review harness; rubrics in `evaluations/rubrics/`). Built for the Google.org Impact Challenge.

The **`output_ledger`** SQLite table is the spine of evaluation: every research artifact written by chat or any scheduled job is logged with provenance (model, tokens, project, cited DOIs, critic score). Treat this as the audit trail. Don't write artifacts that bypass it.

**Public aquarium:** scheduled tool calls are vagueized through `agent/privacy.py` (whitelist of public-research tools, denylist for everything else) and pushed to a Cloudflare Worker, displayed at `coleoguy.github.io/tealc.html`. Source-side leak detection runs nightly (`aquarium_audit.py`).

---

## 3. The `agent/` package — what's where

Critical modules (read these before changing them):

| File | What it does |
|---|---|
| `graph.py` | LangGraph agent + system prompt. The system prompt has a **static prefix** (cached via `cache_control=ephemeral`) and **dynamic addenda** built per chat-start from `data/personality_addendum.md`, `data/heath_preferences.md`, the `goals` table, and the lab Drive layout. Edit `SYSTEM_PROMPT` for stable content; add to the dynamic loaders for things that should refresh per session. |
| `tools.py` | ~7000 lines, ~200 LangChain `@tool` functions. Grouped by domain (literature / Google / R+Python / lab state / external science / etc.). Tool docstrings are exposed to the model — they are the spec. |
| `scheduler.py` | APScheduler process. Defines the 40+ DB tables and runs idempotent migrations on every boot. Registers jobs via `register_job(name, trigger, fn, idle_gate=...)`. |
| `config.py` | `data/tealc_config.json` reader/writer with deterministic 25%/hour sampling for `reduced` jobs. Five named presets (`balanced`, `grant_crunch`, `student_focus`, `research_deep_dive`, `quiet_week`). Hot path — keep it cheap. |
| `model_router.py` | Task → `(model, effort)` mapping. `_OPUS_TASKS`, `_HAIKU_TASKS`, `_SONNET_TASKS`, `EFFORT_TIERS`. `choose_model(task)` returns `ModelChoice(model, effort)`. Logged to `model_routing_decisions`. **This is the single place** to add new task→model rules. |
| `observability.py` | NEW. Opt-in Langfuse tracing. `@traced(name=..., **metadata)` decorator. No-op if `LANGFUSE_*` env vars unset or package missing. Always safe to add. |
| `memory_backend.py` | NEW. File-backed Anthropic Memory tool implementation (`BetaAbstractMemoryTool`). Storage at `~/Library/Application Support/tealc/memories/`. Path-traversal hardened. Atomic writes. |
| `project_sessions.py` | NEW. Per-project filesystem continuity (Anthropic multi-session pattern): `progress.md` + `feature_list.json` per project. `start_project_session()` / `end_project_session()`. |
| `hypothesis_pipeline.py` | Typed-claim gate. Tier 0 (regex smoke) → Tier 1 (Haiku classifier) → Tier 2 (Sonnet/Opus rubric, type-aware). `record_hypothesis` and `adopt_hypothesis` enforce gate verdicts. |
| `voice_index.py` | TF-IDF + embedding index over Heath's published prose. Source: `publications.json` from `lab-pages` repo. Used by drafter jobs and the `retrieve_voice_exemplars` tool. Sklearn fallback to hand-rolled TF-IDF if missing. |
| `ledger.py` | Output ledger writer. Every artifact: `kind`, `job_name`, `model`, `project_id`, `content_md`, `tokens_in`, `tokens_out`, `provenance_json`, `critic_score`. |
| `critic.py` | Adversarial critic framework (Opus by default). Scores 1–5, flags unsupported claims, hype language, missing citations. |
| `privacy.py` | Aquarium event vagueizer. Whitelist of public research tools; everything else gets sanitized. Update both the whitelist AND the test cases when adding a tool that should expose specifics. |
| `dashboard_server.py` | FastAPI dashboard at `:8001`. Reads from `dashboard_state.json`, `abilities.json`, DB. Runs jobs on demand via `/api/run_job`. |

Subdirectories:
- `agent/jobs/` — one file per scheduled job. Pattern: load `.env`, get Anthropic client, build prompt, call model, write to ledger + briefings, wrap in `@traced(...)`. Always idempotent — jobs may be re-fired on demand from the dashboard.
- `agent/apis/` — wrappers for external research APIs (Crossref, Europe PMC, OpenAlex, Semantic Scholar, NCBI, GBIF, OpenTree, GitHub, Zenodo).
- `agent/skills/` — six on-demand SKILL.md playbooks (progressive disclosure). Loaded via `read_local_file` in chat when needed. Don't put behavior here that all sessions need — that goes in the system prompt.
- `agent/prompts/` — six markdown prompt templates used by jobs (citation_proposer, finding_extractor, repo_note_writer, etc.).
- `agent/python_runtime/` and `agent/r_runtime/` — sandbox executors. **Not hardened.** Heath is the sole operator. Don't expose them publicly.

---

## 4. Conventions you must follow

### SQLite
- Always `PRAGMA journal_mode=WAL` immediately after opening a connection (`scheduler.py` and `project_sessions.py` are the canonical examples). The DB lives across many concurrent processes (chat, scheduler, dashboard).
- Schema migrations go in `scheduler.py`'s init path and **must be idempotent** (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` via try/except). The scheduler restarts often.
- Don't introduce a new database. Add a table.

### Atomic file writes
- Use the temp-file + `os.replace` pattern. See `memory_backend.py:_atomic_write` and `config.py`'s `.tmp` writer. Never write directly to a path the running scheduler reads.

### Env vars
- `.env` lives at the install root and is loaded via `load_dotenv` at module top. **Scheduler subprocesses do NOT inherit env from the chat app** — every job that needs env must `load_dotenv` itself (most existing jobs do this).
- After editing `.env`, **restart the scheduler** to pick it up. Running scheduler caches values from startup.

### Job pattern
A new scheduled job is a new file in `agent/jobs/`. Minimum recipe:
```python
from agent.observability import traced
from agent.scheduler import register_job, DB_PATH
# ... imports

@traced(name="my_new_job", job="my_new_job")
def run() -> None:
    # idempotent body. Read state from DB, call model, write to
    # output_ledger and/or briefings. Log to scheduler.log on error.
    ...

# at module load:
register_job(
    name="my_new_job",
    trigger=CronTrigger(hour=4, minute=30, timezone="America/Chicago"),
    fn=run,
    idle_gate=True,  # set True if the job is resource-heavy
)
```
Then add an entry in `agent/config.py`'s default-mode dict so it can be toggled `normal`/`reduced`/`off`.

### Tool pattern
A new chat tool goes in `agent/tools.py` (or a thin wrapper there delegating to a helper module). Minimum recipe:
```python
@tool
def my_new_tool(arg: str, confirmed: bool = False) -> str:
    """One-line summary the model will read in its tool list.

    Multi-line docstring describing parameters, return shape, and any
    pre-conditions (e.g. "call require_data_resource first"). The
    docstring IS the spec — be precise.
    """
    # if destructive: implement preview-then-confirm pattern (see
    # replace_in_google_doc, update_sheet_cells, delete_calendar_event).
    ...
```
Tools that touch external surfaces (Sheets writes, Doc edits, Calendar invites, email trash) **must** use the preview-then-confirm pattern (`confirmed=False` returns a preview; `confirmed=True` executes). This is enforced at the tool level, not by the prompt.

### Model routing
- Don't hand-pick a model in a job. Call `choose_model(task_name)` from `agent/model_router.py`. If the task isn't routed yet, add it to the appropriate constant.
- New Opus tasks need a deliberate justification — Opus is ~5x Sonnet cost. The bar is "Sonnet's output is materially worse on canonical examples."

### Cost & observability
- Wrap any new Anthropic-calling job in `@traced(...)`. Cheap if Langfuse isn't installed, full per-call observability if it is.
- Costs go to `cost_tracking` automatically when models are called via the standard wrappers. Don't bypass.

---

## 5. Hard "do not" list

These are not suggestions — each entry has a real incident behind it.

- **Do not move `agent.db` back into Drive.** Drive sync zeroed it twice. The off-Drive location is intentional.
- **Do not skip `require_data_resource(key)`** before generating R/Python that reads a lab database. On 2026-04-21 a Fragile-Y preregistration silently referenced `<TO-BE-FILLED>` because of this. There is now a code-level guard but the prompt rule is the first defense.
- **Do not bypass the destructive-confirm pattern.** `replace_in_google_doc`, `update_sheet_cells`, and `delete_calendar_event` all require `confirmed=True` on a second call. This is a hard guard. Don't try to route around it.
- **Do not call `create_calendar_event(send_invitations=True)` without explicit Heath authorization in the conversation.** Surprise meeting invites are unacceptable.
- **Do not mass-update curated lab Sheets** (Coleoptera, Diptera, Tree of Sex, Epistasis, etc.) without read-then-diff-then-confirm. These represent years of curation.
- **Do not push system-prompt content with personalized lab/student/grant info to GitHub** without Heath's review. The publishable subset has redactions.
- **Do not assume `/bin/bash` has Full Disk Access in launchd context** — the LaunchAgent silently fails without it. The wrapper script and FDA grant are part of the install. If the scheduler "isn't running" but `Start Tealc.app` works, suspect FDA.
- **Do not narrate every tool call in chat**. Heath wants brevity. The system prompt's `<stance>` and `<behavior>` blocks are the contract.

---

## 6. How Heath works (style guidance)

- He's a senior comparative biologist (chromosome evolution, sex chromosomes, karyotype change, Fragile Y Hypothesis). He reads code well enough to follow what you're doing but does not want to be tutored on Python idioms.
- His top stated priorities: (1) national recognition, (2) higher-ed administration, (3) outstanding mentor reputation. Service requests get the recognition test — does this advance the trajectory or protect students?
- Default to skeptical reading. When he shows you a draft or claim, find the weakest link first. Validation-forward openers ("great question") are noise to him.
- Calibrate uncertainty. "I think" / "my guess is" beat assertion. Don't invent paths, IDs, citations, or facts — investigate or say "I don't know."
- For complex requests (multiple work streams, >3 tool calls, architectural choices), pause for ONE sentence to acknowledge scope before kicking off. Then do the work. Don't ritualize this on every message — only when the work is genuinely large.
- Drafts > 1000 words go to a new Google Doc, not chat. Tealc Drafts folder is configured in `data/config.json`.

---

## 7. Testing & evaluation reality

**Current state (May 2026):** one unit-test file (`tests/test_model_router_effort.py`, 169 lines, `unittest`). No integration tests. No regression suite for jobs or tools. Coverage of the actual production behavior is essentially zero.

This is the single biggest gap between "operational" and "rigorous."

There IS a serious evaluation harness — `evaluations/` — but it's an **external review** harness (blinded → human reviewers → reconciled scores), not a regression suite. It catches drift in artifact quality after the fact. It does not catch broken job code on push.

When you change anything in `agent/scheduler.py`, `agent/tools.py`, or `agent/graph.py`, you cannot rely on tests to catch a regression. **Run the affected job manually after the change** (the dashboard's `/api/run_job` or `run_scheduled_job(name=...)` works). Verify output makes it into `output_ledger` with sane content. This is currently the best safety net we have.

---

## 8. Open lines of work — high-leverage things you can autonomously lead

These are research/engineering programs that would meaningfully improve Tealc, ranked roughly by impact-per-effort. Pick one with Heath, propose a plan, then execute. Don't kick off any of these without alignment.

### A. Regression eval suite (highest priority)
Build a fixed set of canonical scenarios — known inputs with rubric-graded expected outputs — and run them on every code change. Cover the high-stakes paths first: `weekly_hypothesis_generator`, `nightly_grant_drafter`, `email_triage`, `record_chat_artifact`. Use the `output_ledger` as the substrate; LLM-as-judge with periodic human anchoring (every Nth eval pulled into the existing blinded review pipeline to keep the judge calibrated). Without this, every code change is a guess.

### B. Tool-use telemetry & failure-mode taxonomy
Mine `output_ledger`, `cost_tracking`, `model_routing_decisions`, `job_runs`, and `scheduler.log` to produce: per-tool call count / success rate / latency / cost; per-job error distribution; "silent failure" detection (job succeeded, critic score < 2). Build a categorized failure taxonomy (API transient / tool error / prompt error / env / design) and a regression test for each recurring class. This converts ambient observation into actionable signal.

### C. Hypothesis-pipeline calibration study
Take ~50 historical hypothesis proposals (from `hypothesis_proposals` and `output_ledger` with `kind='hypothesis'`). Have Heath grade each as adopt/refine/reject, blinded to the pipeline's verdict. Compute confusion matrix vs the Tier-2 critic. Quantify Opus-vs-Sonnet ROI on the borderline tier. Tune rubric item weights to maximize agreement. Without this, the pipeline is a heuristic, not a calibrated instrument.

### D. Retrieval quality benchmarks
Build a small (50–100 query) labeled benchmark for `search_pubmed`, `search_biorxiv`, `search_openalex`, `find_resource`, `retrieve_voice_exemplars`. Measure precision@5 and recall@20 for each. Track over time. This turns the existing `retrieval_quality_monitor` from a sampling probe into a controlled measurement.

### E. Cost-quality Pareto analysis
For each scheduled job: distribution of `critic_score` vs `cost_usd`. Identify jobs in the high-cost / low-quality quadrant (kill or downgrade to Haiku) and high-quality / low-cost (scale up frequency or generalize the pattern to other jobs). This is achievable in a few hours of analysis and probably saves 15–40% on monthly Anthropic spend at constant output quality.

### F. Reproducibility audit
Pick one published artifact (a synthesis, an adopted hypothesis, a drafted grant section). Try to reproduce it from inputs across (1) the same model build, (2) a different date, (3) Sonnet vs Opus. Quantify variance. The lab values reproducibility — Tealc should be able to defend its own.

### G. "AI scientist" pre-registration loop (the moonshot)
Tealc generates a hypothesis → Heath blesses it → it goes to `prereg_artifacts` with a falsifiable prediction and a check date. At T+horizon, the pre-reg adjudicator runs and scores the prediction against reality. Calibration plot built quarterly. **This is the only operational definition of "AI scientist" that maps to Heath's Google.org pitch.** The infrastructure is already partially there (`prereg_artifacts`, `prereg_replication_loop.py`); what's missing is the discipline of actually closing the loop and reporting it.

### H. Skill triggering and progressive-disclosure measurement
Six SKILL.md files exist; they're loaded only when the model decides a task needs one. Measure: when the model SHOULD have loaded a skill but didn't (false negative), and when it loaded one it didn't need (false positive). The skill description text is the lever — tune it against measured triggering.

### I. tools.py refactor (lower priority but real debt)
7000 lines in one file. Splitting by domain (literature, google, lab_state, science_apis, sandboxes) reduces cognitive load and makes future tool additions safer. Risk: a careless refactor breaks imports across 77 jobs and the chat graph. Approach: domain-by-domain, with a shim that re-exports from `agent.tools` so call sites don't change in step 1; remove the shim later.

### J. Privacy classifier audit
`privacy.py`'s vagueizer is whitelist-based. Sample 100 random aquarium events and verify by hand that no private content leaked. Existing `aquarium_audit` job samples post-hoc; this should be a scheduled adversarial test, not a hope.

---

## 9. Default working stance

When a request comes in:

1. If it touches the live system (chat behavior, scheduler, system prompt, tools, jobs), edit the **Drive** copy. If it's a publishable change, mirror to GitHub afterward.
2. If you're not sure where state lives, look it up — don't guess. The DB is `~/Library/Application Support/tealc/agent.db`. The schema is in `agent/scheduler.py`.
3. If you're about to introduce a new abstraction (a new module, a new table, a new env var, a new top-level file), pause and ask whether the existing structure already covers it. Tealc has a lot of moving parts; restraint about adding new ones is high-value.
4. If a job or tool change could affect output quality (drafter, hypothesis, synthesis, critic), run it manually post-change and inspect the `output_ledger` row. We don't have regression tests yet (see §7).
5. If you find a bug or design issue that's out of scope for the current task, note it but don't silently fix it as a "while I'm here" — that bloats diffs Heath has to review. Surface it; let him decide.
6. If something seems unused or dead, do not delete in passing. The codebase has a lot of half-finished branches that are intentional ground — `personas/`, parts of `subagents.py`, etc. Confirm before removing.

When in doubt, ask. Heath will tell you whether the question is worth his attention or whether to use judgment.
