# Tealc V2 Helper Modules

Foundation-layer modules added for Google.org Impact Challenge evaluation readiness.
All modules follow the WAL-mode, fresh-connection-per-call SQLite convention.

---

## 1. `agent/cost_tracking.py`

**Purpose:** Records every Anthropic API call with token counts and estimated USD cost.
Enables spend accountability and cache-hit rate tracking for the eval harness.

**Key functions:**
- `record_call(job_name: str, model: str, usage: dict) -> None`
  Writes one row to `cost_tracking`. `usage` keys: `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens` (any subset tolerated).
- `summarize_costs(since_iso: str | None, job_name: str | None) -> dict`
  Returns `{total_usd, total_calls, by_model, by_job, cache_hit_rate}`.

**Example:**
```python
from agent.cost_tracking import record_call, summarize_costs
record_call("nightly_lit", "claude-sonnet-4-6", msg.usage.__dict__)
print(summarize_costs(since_iso="2026-04-01T00:00:00+00:00"))
```

**DB table:** `cost_tracking`

---

## 2. `agent/model_router.py`

**Purpose:** Centralises model selection so every job uses the right tier (Opus/Sonnet/Haiku)
and the right reasoning depth (effort tier for Claude 4.6+ adaptive thinking). Every
decision is logged for audit. Eliminates per-job hard-coded model strings AND per-job
hard-coded thinking-effort settings.

**Constants:** `SONNET = "claude-sonnet-4-6"`, `OPUS = "claude-opus-4-7"`,
`HAIKU = "claude-haiku-4-5-20251001"`. Plus `EFFORT_TIERS: dict[str, str]` mapping
task type → effort level.

**Return type:** `ModelChoice = NamedTuple("ModelChoice", model: str, effort: str)`.
Tuple-unpacking and attribute access both work: `model, effort = choose_model(...)` or
`choice.model` / `choice.effort`.

**Key function:**
- `choose_model(task_type: str, complexity_hint: str | None = None, require_opus: bool = False, log: bool = True) -> ModelChoice`
  Returns `(model, effort)`. If `log=True` (default), inserts a routing decision row.
  Unknown task types default to `(SONNET, "medium")`.

**Effort tiers:**
- `xhigh`: cross_project_synthesis, run_formal_hypothesis_pass, pre_submission_review,
  opus_critic, hypothesis_critique
- `high`: chat (default), nightly_grant_drafter, weekly_comparative_analysis,
  morning_briefing
- `medium`: weekly_hypothesis_generator, nightly_literature_synthesis, daily_plan,
  midday_check, nas_pipeline_health, nas_impact_score, weekly_review
- `low`: email_triage, paper_of_the_day, midday_lit_pulse, vip_email_watch, executive,
  populate_project_keywords, citation_watch, paper_radar
- Unknown → `medium` (logged)

**Example:**
```python
from agent.model_router import choose_model
choice = choose_model("cross_project_synthesis")  # ModelChoice(model='claude-sonnet-4-6', effort='xhigh')
model, effort = choose_model("email_triage")      # ('claude-sonnet-4-6', 'low')
choice = choose_model("my_new_job", log=False)    # ModelChoice('...', 'medium')
```

**Tests:** `tests/test_model_router_effort.py` (18 cases).

**DB table:** `model_routing_decisions`

---

## 3. `agent/ledger.py`

**Purpose:** Full provenance tracking for every research artifact Tealc produces —
grants, hypotheses, literature syntheses, analyses. Stores tool calls used, papers cited,
and context snapshot. Supports critic scoring and Heath's adopt/reject/ignore decisions.

**Key functions:**
- `record_output(kind, job_name, model, project_id, content_md, tokens_in, tokens_out, provenance) -> int`
  Inserts and returns `row_id`. `provenance` expected keys: `tool_calls`, `papers_cited`, `context_snapshot`.
- `update_critic(row_id, score, notes, model) -> None` — after critic pass.
- `update_user_action(row_id, action, reason) -> None` — action in `{"adopted","rejected","ignored"}`.
- `query_outputs(kind, since_iso, until_iso, min_score, project_id, limit) -> list[dict]`
- `get_entry(row_id) -> dict | None`

**Example:**
```python
from agent.ledger import record_output, update_critic, update_user_action
rid = record_output("hypothesis", "weekly_hypothesis_generator", "claude-sonnet-4-6",
                    "proj_beetles", content_md="...", tokens_in=800, tokens_out=500,
                    provenance={"tool_calls": [], "papers_cited": ["10.1234/x"], "context_snapshot": {}})
update_critic(rid, score=4, notes="Minor hedging issues.", model="claude-opus-4-7")
update_user_action(rid, "adopted", reason="Aligns with current lab direction")
```

**DB table:** `output_ledger`

---

## 4. `agent/critic.py`

**Purpose:** Adversarial critic pass using Claude Opus 4.7 with prompt caching on the
static rubric. Scores research outputs 1-5 on rigor, calibration, citation grounding,
and hype avoidance. Called after every significant artifact is generated.

**Key function:**
- `critic_pass(draft_text: str, rubric_name: str = "default") -> dict`
  Returns `{score, unsupported_claims, missing_citations, hype_flags, calibration_notes,
  overall_notes, model, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens}`.
  Automatically calls `cost_tracking.record_call`.

**Rubrics:** `"default"`, `"grant_draft"`, `"hypothesis"`, `"analysis"`

**Prompt caching:** The rubric block is marked `cache_control: ephemeral`, so repeated
calls within 5 minutes amortize the rubric input cost (~70% cache hit rate expected).

**Example:**
```python
from agent.critic import critic_pass
from agent.ledger import update_critic
result = critic_pass(draft_text=my_hypothesis, rubric_name="hypothesis")
update_critic(row_id, score=result["score"], notes=result["overall_notes"],
              model=result["model"])
```

**DB tables:** `cost_tracking` (via `record_call`); results stored in `output_ledger` via `ledger.update_critic`

---

## 5. `agent/bundle.py`

**Purpose:** Packages analysis runs into self-contained `tar.gz` archives for
reproducibility. Each bundle includes the R script, session info, results JSON,
interpretation, a README with reproduction steps, and SHA256-verified data files.

**Key functions:**
- `package_analysis_run(run_id: int) -> str`
  Reads `analysis_runs` row, writes `data/r_runs/bundles/analysis_{run_id}_{ts}.tar.gz`,
  returns absolute path.
- `list_bundles(limit: int = 50) -> list[dict]`
  Returns `[{path, bytes, created_iso, run_id}]` newest-first.

**Example:**
```python
from agent.bundle import package_analysis_run, list_bundles
path = package_analysis_run(run_id=42)
print(f"Bundle created: {path}")
for b in list_bundles(limit=5):
    print(b["path"], b["bytes"])
```

**DB table:** `analysis_runs` (read-only); bundles stored in `data/r_runs/bundles/`

---

## 6. `agent/jobs/__init__.py` — `SCIENTIST_MODE` preamble

**Purpose:** Shared system-prompt preamble (~200 tokens, ~1.2KB) prepended to the
prompts of every artifact-grade scheduled job. Enforces a calibration / anti-hype /
anti-fabrication floor across all LLM-using jobs.

**Constant:** `SCIENTIST_MODE: str` — covers calibrate, no-hype (banned word list),
distinguish hypothesis-vs-finding / correlation-vs-causation, don't-fabricate, be-terse.

**Wired into:** `nightly_grant_drafter`, `weekly_hypothesis_generator`,
`nightly_literature_synthesis`, `weekly_comparative_analysis` interpreter,
`nas_impact_score`, `nas_pipeline_health`, `cross_project_synthesis`, `weekly_review`.

**Pattern for new jobs:**
```python
from agent.jobs import tracked, SCIENTIST_MODE

_SYSTEM = SCIENTIST_MODE + "\n\n" + (
    "Your job-specific instructions here..."
)
```

The everyday/conversational jobs (morning_briefing, email_triage, paper_of_the_day,
meeting_prep, etc.) deliberately do NOT prepend SCIENTIST_MODE — their outputs aren't
artifact-grade and the preamble would add cost without proportional behavioral lift.

---

## 7. `agent/memory_backend.py`

**Purpose:** Tealc's implementation of Anthropic's [Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool).
Gives the chat agent a `/memories` filesystem it manages itself across sessions —
useful for project-level scratchpads, multi-week work tracking, and the
multi-session harness pattern.

**Class:** `TealcMemoryTool(BetaAbstractMemoryTool)` from `anthropic.lib.tools`.

**Storage path:** `~/Library/Application Support/tealc/memories/` (override via
`TEALC_MEMORY_DIR` env var). Lives outside Google Drive — same logic as `agent.db`,
to avoid Drive sync corruption.

**Commands:** `view`, `create`, `str_replace`, `insert`, `delete`, `rename` — all
per Anthropic's spec, with line-numbered file reads and unique-match enforcement on
`str_replace`.

**Safety:** path-traversal protection (paths must start with `/memories`, resolve
within the storage root, no `../`, no URL-encoded escapes), 200KB file cap, 200-char
filename cap, 50K-line cap, atomic writes via temp-file + rename.

**Tool spec (for API registration):** `MEMORY_TOOL_SPEC = {"type": "memory_20250818", "name": "memory"}`.

**Status:** module ready; native registration in `build_graph` deferred (langchain-anthropic
needs additional integration for the `memory_20250818` beta tool format).

---

## 8. `agent/project_sessions.py`

**Purpose:** Anthropic's [multi-session harness pattern](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
applied to research projects. Each project gets a `progress.md` log + optional
`feature_list.json` + optional `init.sh`, stored at
`~/Library/Application Support/tealc/memories/projects/<project_id>/`.

**Key functions:**
- `start_project_session(project_id) -> dict` — opens session: reads or creates
  `progress.md` from a template, parses `feature_list.json` if present, returns the
  project's SQLite row + raw markdown + parsed feature list.
- `end_project_session(project_id, completed_md, remaining_md, notes_md) -> dict` —
  appends a timestamped section to `progress.md`, updates
  `research_projects.last_touched_iso`.

**Wrapped as @tool:** Both functions are wrapped as `@tool start_project_session`
and `@tool end_project_session` in `agent/tools.py`, callable from chat. Use at the
start/end of any extended work session on a specific project.

---

## 9. `agent/observability.py`

**Purpose:** Optional [Langfuse](https://langfuse.com/) tracing for LLM calls. No-op
when langfuse isn't installed or env vars are unset — does NOT crash anything.

**Public API:**
- `enabled() -> bool` — quick guard check.
- `get_langfuse_client()` — memoized lazy initializer; returns `None` if unconfigured.
- `@traced(name, **metadata)` — decorator that wraps an Anthropic-calling function
  with a Langfuse trace/generation; pure pass-through when disabled.
- `score_output(trace_id, name, value, comment)` — record LLM-as-judge or human
  review scores.

**Setup:** `pip install langfuse` (NOT in requirements.txt — opt-in only). Set
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, optionally `LANGFUSE_HOST` in `.env`.

**Recommended first wraps:** `_extract_findings` in `nightly_literature_synthesis`,
the drafter call in `nightly_grant_drafter`, the Sonnet call in
`weekly_hypothesis_generator`, `_rewrite_lead` in `surface_composer`,
`_synthesise_briefing` in `morning_briefing`.

---

## 10. `agent/skills/`

**Purpose:** [Anthropic Agent Skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
— on-demand SKILL.md files that the chat agent reads via `read_local_file` only when
relevant. Progressive disclosure: ~100 tokens of metadata in the system prompt,
1.5–3K tokens of body content loaded only when triggered.

**Skills shipped:**

| Skill | Trigger | Body size |
|---|---|---|
| `karyotype-databases/` | working with karyotype/chromosome-number data | ~1,380 tokens |
| `r-comparative-phylogenetics/` | writing R code for comparative phylogenetics | ~1,680 tokens |
| `wiki-authoring/` | authoring or editing pages under `/knowledge/` | ~1,840 tokens |
| `grant-section-drafter/` | drafting grant/manuscript sections in the researcher's voice | ~1,814 tokens |
| `hypothesis-pipeline-rubric/` | proposing, evaluating, or critiquing a testable claim | ~2,762 tokens |
| `voice-matching/` | extended prose meant to read as the researcher's writing | ~1,699 tokens |

**Discovery:** the `<skills>` block at the top of the system prompt lists all six with
their triggers. The chat agent reads the relevant SKILL.md once per session per skill.

**Migration note:** several sections of `agent/graph.py` SYSTEM_PROMPT are now redundant
with these skills (the SHEETS SAFETY block, R EXECUTION block, LAB WIKI section,
hypothesis-proposal workflow, extended-prose-as-Heath workflow). A future cleanup pass
will trim these for ~1,500 tokens of static-prefix savings.
