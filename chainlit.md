# Tealc

A lab member working alongside the Blackmon Lab at Texas A&M.

**Connected**

`gmail` · `calendar` · `drive` · `docs` · `sheets` · `pubmed` · `europepmc` · `openalex` · `biorxiv` · `semantic-scholar` · `ncbi` · `gbif` · `opentree` · `zenodo` · `github`

Plus: R and Python sandboxes for analysis, the lab's curated karyotype databases (Coleoptera, Diptera, Tree of Sex, …), and a voice index over your own published prose for style-matching.

**File attachments**

Drop a PDF, DOCX, or text/CSV/MD into the chat and Tealc will read it.

**Models**

Sonnet 4.6 by default. Say **"think hard"**, **"use opus"**, or **"deep thinking"** to switch to Opus 4.7 for harder reasoning.

**Background loop**

A separate scheduler runs ~50 jobs at daily, nightly, weekly, and quarterly cadences — morning/midday briefings, deadline countdowns, paper-of-the-day, nightly literature synthesis and grant drafting (idle-window only), weekly hypothesis generation and comparative R analyses, weekly self-review, national-recognition impact scoring, database health checks, more. Briefings produced overnight surface at the top of each new chat. Configure via `data/tealc_config.json` or one of the named presets (`balanced`, `grant_crunch`, `student_focus`, `research_deep_dive`, `quiet_week`).

**State**

Goals, milestones, today's plan, decisions log, students, intentions, research projects, grants, hypothesis proposals, literature notes, output ledger — all in `data/agent.db`. Conversation memory persists across sessions; past threads are summarized and full-text searchable via `recall_past_conversations`.

**Hypothesis pipeline**

Any testable claim that surfaces in chat (or a scheduled job) can run through a typed gate: Tier 0 smoke filter → Haiku classifier → Sonnet/Opus type-aware critic. Adoptable hypotheses are tracked; failed gates record their reasons.

**Command Center**

[Tealc HQ](http://localhost:8001) — local dashboard with tasks, activity, and abilities. There's a quick link below the lab logo at left.

**Ask Tealc directly**

- *"What can you do for me?"* — full programmatic summary of every tool and scheduled job, grouped by category + cadence.
- *"Run <job_name> now"* — force-runs any background job immediately, bypassing its working-hours guard (e.g. *"run wiki_janitor now"*, *"trigger paper_of_the_day"*).

---

Part of the [Blackmon Lab](https://coleoguy.github.io) research system.
