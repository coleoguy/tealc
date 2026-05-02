"""Unit tests for the effort parameter wiring in choose_model()."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Bootstrap: load agent/model_router.py without needing the real scheduler/DB
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent  # .../tealc/


def _load_model_router():
    """Load agent.model_router by file path, stubbing agent.scheduler."""
    # Ensure there's a minimal "agent" package in sys.modules so relative
    # imports inside model_router work.
    if "agent" not in sys.modules or not hasattr(sys.modules["agent"], "__path__"):
        pkg = types.ModuleType("agent")
        pkg.__path__ = [str(_REPO_ROOT / "agent")]
        pkg.__package__ = "agent"
        sys.modules["agent"] = pkg

    # Stub agent.scheduler so DB_PATH resolves without touching the filesystem.
    sched = types.ModuleType("agent.scheduler")
    sched.DB_PATH = ":memory:"
    sys.modules["agent.scheduler"] = sched

    spec = importlib.util.spec_from_file_location(
        "agent.model_router",
        str(_REPO_ROOT / "agent" / "model_router.py"),
        submodule_search_locations=[],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent.model_router"] = mod
    spec.loader.exec_module(mod)
    return mod


mr = _load_model_router()
ModelChoice = mr.ModelChoice
SONNET = mr.SONNET
OPUS = mr.OPUS
HAIKU = mr.HAIKU


# ---------------------------------------------------------------------------
# Helper: call choose_model with DB logging disabled
# ---------------------------------------------------------------------------
def _call(task_type, **kwargs):
    return mr.choose_model(task_type, log=False, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestModelChoiceNamedTuple(unittest.TestCase):
    """ModelChoice is a NamedTuple — check attribute and index access."""

    def test_attribute_access(self):
        c = ModelChoice(model=SONNET, effort="high")
        self.assertEqual(c.model, SONNET)
        self.assertEqual(c.effort, "high")

    def test_index_access(self):
        c = ModelChoice(model=SONNET, effort="high")
        self.assertEqual(c[0], SONNET)
        self.assertEqual(c[1], "high")

    def test_tuple_unpacking(self):
        model, effort = ModelChoice(model=OPUS, effort="xhigh")
        self.assertEqual(model, OPUS)
        self.assertEqual(effort, "xhigh")


class TestChooseModelReturnsModelChoice(unittest.TestCase):
    """choose_model() returns ModelChoice with correct (model, effort) pairs."""

    # --- return type ---
    def test_returns_model_choice_instance(self):
        self.assertIsInstance(_call("morning_briefing"), ModelChoice)

    # --- known task_type → correct (model, effort) pair ---
    def test_morning_briefing_sonnet_high(self):
        result = _call("morning_briefing")
        self.assertEqual(result.model, SONNET)
        self.assertEqual(result.effort, "high")

    def test_email_triage_haiku_low(self):
        # email_triage is in _HAIKU_TASKS AND EFFORT_TIERS["low"]
        result = _call("email_triage")
        self.assertEqual(result.model, HAIKU)
        self.assertEqual(result.effort, "low")

    def test_cross_project_synthesis_xhigh(self):
        self.assertEqual(_call("cross_project_synthesis").effort, "xhigh")

    def test_daily_plan_medium(self):
        result = _call("daily_plan")
        self.assertEqual(result.model, SONNET)
        self.assertEqual(result.effort, "medium")

    def test_weekly_hypothesis_generator_medium(self):
        self.assertEqual(_call("weekly_hypothesis_generator").effort, "medium")

    def test_paper_radar_low(self):
        self.assertEqual(_call("paper_radar").effort, "low")

    def test_nightly_grant_drafter_high(self):
        self.assertEqual(_call("nightly_grant_drafter").effort, "high")

    def test_formal_hypothesis_pass_xhigh(self):
        self.assertEqual(_call("formal_hypothesis_pass").effort, "xhigh")

    # --- unknown task_type → SONNET + "medium" ---
    def test_unknown_task_defaults_to_sonnet_medium(self):
        result = _call("totally_unknown_task_xyz")
        self.assertEqual(result.model, SONNET)
        self.assertEqual(result.effort, "medium")

    def test_another_unknown_defaults_medium(self):
        self.assertEqual(_call("not_in_any_registry").effort, "medium")

    # --- require_opus override still returns ModelChoice ---
    def test_require_opus_with_known_low_task(self):
        result = _call("email_triage", require_opus=True)
        self.assertEqual(result.model, OPUS)
        self.assertEqual(result.effort, "low")

    def test_require_opus_unknown_task_medium(self):
        result = _call("unknown_task_z", require_opus=True)
        self.assertEqual(result.model, OPUS)
        self.assertEqual(result.effort, "medium")

    # --- all xhigh tasks ---
    def test_all_xhigh_tasks(self):
        for task in [
            "cross_project_synthesis",
            "run_formal_hypothesis_pass",
            "formal_hypothesis_pass",
            "pre_submission_review",
            "opus_critic",
            "hypothesis_critique",
            "weekly_review_critic",
        ]:
            with self.subTest(task=task):
                self.assertEqual(_call(task).effort, "xhigh")

    # --- all low tasks ---
    def test_all_low_tasks(self):
        for task in [
            "email_triage",
            "email_triage_classifier",
            "paper_of_the_day",
            "midday_lit_pulse",
            "vip_email_watch",
            "executive",
            "populate_project_keywords",
            "citation_watch",
            "paper_radar",
        ]:
            with self.subTest(task=task):
                self.assertEqual(_call(task).effort, "low")


if __name__ == "__main__":
    unittest.main()
