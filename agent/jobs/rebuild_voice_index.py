"""Rebuild the TF-IDF voice index from Heath's publications.

Recommended schedule (registered by Wave 3 agent):
    CronTrigger(day_of_week="tue", hour=4, minute=0, timezone="America/Chicago")
    — weekly Tuesdays 4 am Central.

Manual run:
    python -m agent.jobs.rebuild_voice_index
"""
from agent.jobs import tracked
from agent.voice_index import build_index


@tracked("rebuild_voice_index")
def job() -> str:
    """Refresh the voice index and return a one-line summary."""
    try:
        stats = build_index(refresh=True)
        n = stats.get("papers_indexed", 0)
        m = stats.get("total_chars", 0)
        backend = stats.get("backend", "unknown")
        return f"indexed {n} passages, {m} chars total (backend={backend})"
    except Exception as exc:
        return f"error rebuilding voice index: {exc}"


if __name__ == "__main__":
    print(job())
