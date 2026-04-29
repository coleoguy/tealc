"""Tealc config accessor — loads/saves data/tealc_config.json.

Fast path: load_config() is called on every scheduler tick so it must be cheap.
Atomic writes: save_config() writes to .tmp then renames to avoid corruption.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.normpath(os.path.join(_HERE, "..", "data", "tealc_config.json"))
_ADDENDUM_PATH = os.path.normpath(os.path.join(_HERE, "..", "data", "personality_addendum.md"))

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULTS: dict = {
    "version": 1,
    "updated_at": "2026-04-19T00:00:00+00:00",
    "jobs": {
        "morning_briefing":             {"mode": "normal"},
        "midday_check":                 {"mode": "normal"},
        "deadline_countdown":           {"mode": "normal"},
        "meeting_prep":                 {"mode": "normal"},
        "vip_email_watch":              {"mode": "normal"},
        "paper_of_the_day":             {"mode": "normal"},
        "daily_plan":                   {"mode": "normal"},
        "nightly_grant_drafter":        {"mode": "normal"},
        "nightly_literature_synthesis": {"mode": "normal"},
        "weekly_hypothesis_generator":  {"mode": "normal"},
        "weekly_comparative_analysis":  {"mode": "normal"},
        "cross_project_synthesis":      {"mode": "normal"},
        "exploratory_analysis":         {"mode": "normal"},
        "student_agenda_drafter":       {"mode": "normal"},
        "student_pulse":                {"mode": "normal"},
        "nas_pipeline_health":          {"mode": "normal"},
        "nas_case_packet":              {"mode": "normal"},
        "goal_conflict_check":          {"mode": "normal"},
        "weekly_review":                {"mode": "normal"},
        "wiki_janitor":                 {"mode": "normal"},
        "refresh_enrichment":           {"mode": "normal"},
    },
    "thresholds": {
        "stalled_flagship_days":     21,
        "drafter_pause_count":       3,
        "meeting_prep_lead_minutes": 60,
        "working_hours_start":       8,
        "working_hours_end":         22,
        "vip_email_check_minutes":   5,
        "deadline_countdown_days":   10,
    },
    "personality": {
        "bluntness":  0.7,
        "brevity":    0.6,
        "skepticism": 0.5,
    },
    "active_preset": "balanced",
}

# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
PRESETS: dict[str, dict] = {
    "balanced": {
        "jobs": {},  # all normal — handled by apply_preset logic
        "thresholds": {},  # defaults
    },
    "grant_crunch": {
        "jobs": {
            "nightly_grant_drafter":  {"mode": "normal"},
            "deadline_countdown":     {"mode": "normal"},
            "nas_pipeline_health":    {"mode": "normal"},
            "exploratory_analysis":   {"mode": "reduced"},
            "cross_project_synthesis": {"mode": "reduced"},
            "student_agenda_drafter": {"mode": "reduced"},
        },
        "thresholds": {
            "stalled_flagship_days": 10,
            "deadline_countdown_days": 5,
        },
    },
    "student_focus": {
        "jobs": {
            "student_agenda_drafter": {"mode": "normal"},
            "student_pulse":          {"mode": "normal"},
            "midday_check":           {"mode": "normal"},
            "nightly_grant_drafter":  {"mode": "reduced"},
            "exploratory_analysis":   {"mode": "reduced"},
        },
        "thresholds": {},
    },
    "research_deep_dive": {
        "jobs": {
            "exploratory_analysis":         {"mode": "normal"},
            "cross_project_synthesis":      {"mode": "normal"},
            "nightly_literature_synthesis": {"mode": "normal"},
            "weekly_hypothesis_generator":  {"mode": "normal"},
            "midday_check":                 {"mode": "reduced"},
            "meeting_prep":                 {"mode": "reduced"},
            "vip_email_watch":              {"mode": "reduced"},
        },
        "thresholds": {},
    },
    "quiet_week": {
        "jobs": {
            # proactive briefings → reduced
            "morning_briefing":    {"mode": "reduced"},
            "midday_check":        {"mode": "reduced"},
            "deadline_countdown":  {"mode": "reduced"},
            "meeting_prep":        {"mode": "reduced"},
            "vip_email_watch":     {"mode": "reduced"},
            "paper_of_the_day":    {"mode": "reduced"},
            "daily_plan":          {"mode": "reduced"},
            "student_agenda_drafter": {"mode": "reduced"},
            "nas_pipeline_health": {"mode": "reduced"},
            # drafters / synthesis / analysis → off
            "nightly_grant_drafter":        {"mode": "off"},
            "nightly_literature_synthesis": {"mode": "off"},
            "weekly_hypothesis_generator":  {"mode": "off"},
            "weekly_comparative_analysis":  {"mode": "off"},
            "cross_project_synthesis":      {"mode": "off"},
            "exploratory_analysis":         {"mode": "off"},
            "nas_case_packet":              {"mode": "off"},
        },
        "thresholds": {},
    },
}


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load data/tealc_config.json. Return defaults on read error."""
    try:
        with open(_CONFIG_PATH) as f:
            data = json.load(f)
        # Merge any missing keys from defaults
        for key, val in _DEFAULTS.items():
            if key not in data:
                data[key] = val
        return data
    except Exception:
        return dict(_DEFAULTS)


def _regenerate_addendum(cfg: dict) -> None:
    """Write data/personality_addendum.md from current personality values."""
    p = cfg.get("personality", {})
    bluntness = float(p.get("bluntness", 0.7))
    brevity = float(p.get("brevity", 0.6))
    skepticism = float(p.get("skepticism", 0.5))

    # Bluntness bullet
    if bluntness > 0.7:
        bluntness_bullet = (
            "- Lean toward direct, matter-of-fact statements without softening. "
            "Skip \"I'd suggest\" / \"perhaps consider\" — just say what."
        )
    elif bluntness < 0.3:
        bluntness_bullet = (
            "- Soften recommendations with context and alternatives. "
            "Prefer \"one option is...\" over absolutes."
        )
    else:
        bluntness_bullet = (
            "- Balance directness with acknowledgment of uncertainty."
        )

    # Brevity bullet
    if brevity > 0.7:
        brevity_bullet = (
            "- Keep responses tight. Short paragraphs. No throat-clearing."
        )
    elif brevity < 0.3:
        brevity_bullet = (
            "- Feel free to elaborate; fuller explanations and examples are welcome."
        )
    else:
        brevity_bullet = (
            "- Match response length to the complexity of the question — "
            "neither terse nor verbose."
        )

    # Skepticism bullet
    if skepticism > 0.7:
        skepticism_bullet = (
            "- Apply a healthy skepticism to your own suggestions and to any unverified claims; "
            "name the assumption you're making."
        )
    elif skepticism < 0.3:
        skepticism_bullet = (
            "- Lean optimistic. Engage with ideas as stated; save caveats for genuine risks."
        )
    else:
        skepticism_bullet = (
            "- Apply a moderately healthy skepticism to your own suggestions and to any "
            "unverified claims; name the assumption you're making."
        )

    content = (
        f"# Personality tuning (active at chat start)\n\n"
        f"Bluntness: {bluntness:.2f} (1.0 = very blunt, 0.0 = very gentle)\n"
        f"Brevity: {brevity:.2f} (1.0 = terse, 0.0 = verbose)\n"
        f"Skepticism: {skepticism:.2f} (1.0 = skeptical, 0.0 = optimistic)\n\n"
        f"Behavioral guidance:\n"
        f"{bluntness_bullet}\n"
        f"{brevity_bullet}\n"
        f"{skepticism_bullet}\n"
    )

    tmp = _ADDENDUM_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, _ADDENDUM_PATH)
    except Exception:
        pass  # addendum failure must not block callers


def save_config(cfg: dict) -> None:
    """Atomic write — write to .tmp then rename. Updates updated_at.
    Also regenerates data/personality_addendum.md from personality values."""
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = _CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, _CONFIG_PATH)
    _regenerate_addendum(cfg)


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def is_job_enabled(job_name: str) -> bool:
    """True if mode != 'off'."""
    cfg = load_config()
    mode = cfg.get("jobs", {}).get(job_name, {}).get("mode", "normal")
    return mode != "off"


def should_run_this_cycle(job_name: str) -> bool:
    """For 'reduced' mode, returns True ~25% of the time via deterministic hash
    of (job_name + yyyy-mm-dd-hh). For 'normal' → always True. For 'off' → False.
    """
    cfg = load_config()
    mode = cfg.get("jobs", {}).get(job_name, {}).get("mode", "normal")
    if mode == "off":
        return False
    if mode == "normal":
        return True
    # reduced — deterministic 25% sample keyed to job + current hour bucket
    bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
    key = f"{job_name}:{bucket}"
    digest = int(hashlib.md5(key.encode()).hexdigest(), 16)  # noqa: S324
    return (digest % 4) == 0


# ---------------------------------------------------------------------------
# Threshold accessor
# ---------------------------------------------------------------------------

def get_threshold(name: str, default=None):
    """Read thresholds[name] with fallback."""
    cfg = load_config()
    return cfg.get("thresholds", {}).get(name, default)


# ---------------------------------------------------------------------------
# Preset application
# ---------------------------------------------------------------------------

def apply_preset(preset_name: str) -> dict:
    """Mutate config to match a named preset. Returns updated config (not yet saved)."""
    if preset_name not in PRESETS:
        raise ValueError(
            f"Unknown preset {preset_name!r}. Valid: {sorted(PRESETS)}"
        )

    cfg = load_config()
    preset = PRESETS[preset_name]

    if preset_name == "balanced":
        # Reset all jobs to normal and thresholds to defaults
        for job in cfg.get("jobs", {}):
            cfg["jobs"][job] = {"mode": "normal"}
        cfg["thresholds"].update(_DEFAULTS["thresholds"])
    else:
        # Start from the current config, apply only the preset overrides
        for job, job_cfg in preset.get("jobs", {}).items():
            cfg.setdefault("jobs", {})[job] = job_cfg
        for tkey, tval in preset.get("thresholds", {}).items():
            cfg.setdefault("thresholds", {})[tkey] = tval

    cfg["active_preset"] = preset_name
    return cfg
