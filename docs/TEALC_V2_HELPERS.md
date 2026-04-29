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
and every decision is logged for audit. Eliminates per-job hard-coded model strings.

**Constants:** `SONNET = "claude-sonnet-4-6"`, `OPUS = "claude-opus-4-7"`,
`HAIKU = "claude-haiku-4-5-20251001"`

**Key function:**
- `choose_model(task_type: str, complexity_hint: str | None, require_opus: bool, log: bool) -> str`
  Returns model string. If `log=True` (default), inserts a routing decision row.
  Unknown task types default to SONNET.

**Example:**
```python
from agent.model_router import choose_model
model = choose_model("critic_pass")           # -> OPUS
model = choose_model("email_triage")          # -> HAIKU
model = choose_model("morning_briefing")      # -> SONNET
model = choose_model("my_new_job", log=False) # -> SONNET, no DB write
```

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
