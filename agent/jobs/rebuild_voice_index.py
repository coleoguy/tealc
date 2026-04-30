"""Rebuild the TF-IDF voice index from Heath's publications.

Also invokes the sentence-embedding corpus builder (build_heath_corpus)
so both retrieval backends stay in sync.

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
    """Refresh the TF-IDF voice index and the sentence-embedding corpus."""
    parts: list[str] = []

    # 1. Legacy TF-IDF rebuild (keeps retrieve_voice_exemplars working)
    try:
        stats = build_index(refresh=True)
        n = stats.get("papers_indexed", 0)
        m = stats.get("total_chars", 0)
        backend = stats.get("backend", "unknown")
        parts.append(f"tfidf: {n} passages, {m} chars (backend={backend})")
    except Exception as exc:
        parts.append(f"tfidf error: {exc}")

    # 2. Sentence-embedding corpus build (Tier-2 foundation)
    try:
        from agent.jobs.build_heath_corpus import job as corpus_job  # noqa: PLC0415
        corpus_summary = corpus_job()
        parts.append(f"corpus: {corpus_summary}")
    except Exception as exc:
        parts.append(f"corpus error: {exc}")

    return " | ".join(parts)


if __name__ == "__main__":
    print(job())
