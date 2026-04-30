"""ask_my_record.py — Answer a question using only Heath's own published papers.

Pipeline:
    1. retrieve_similar_sentences(question, k=18, min_cosine=0.55)
    2. Group by paper_id; pick top max_papers papers (by max similarity)
    3. For each paper, take top sentences_per_paper_cap sentences (capped at max_chars_per_paper)
    4. Build a prompt instructing Sonnet to cite as (Paper N, Year)
    5. Call claude-sonnet-4-6, temperature=0.2, max_tokens=400
    6. Return the answer string with inline citations

Public API
----------
ask_my_record(question, max_papers=6, sentences_per_paper_cap=3, max_chars_per_paper=800) -> str
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

_MODEL = "claude-sonnet-4-6"


def _paper_id_short(paper_id: str) -> str:
    """Return a short slug from a paper_id.

    - Strip 'https://doi.org/' or 'doi.org/' prefix if present.
    - Otherwise take the last 8 characters.
    """
    if not paper_id:
        return "unknown"
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
        if paper_id.startswith(prefix):
            return paper_id[len(prefix):]
    # Fallback: last 8 chars
    return paper_id[-8:] if len(paper_id) > 8 else paper_id


def ask_my_record(
    question: str,
    max_papers: int = 6,
    sentences_per_paper_cap: int = 3,
    max_chars_per_paper: int = 800,
) -> str:
    """Answer a question using only Heath's own 63 papers.

    Pipeline:
        1. retrieve_similar_sentences(question, k=18, min_cosine=0.55)
        2. Group by paper_id; pick top max_papers papers (by max similarity)
        3. For each paper, take top sentences_per_paper_cap (capped at max_chars_per_paper chars)
        4. Build a prompt: "Answer in 200 words using only these passages. Cite as
           (Paper N, Year). Do not invent facts not in the passages."
        5. Call Sonnet 4.6, temperature=0.2, max_tokens=400
        6. Return the answer string with inline (Paper N, Year) citations

    Returns a 200-word synthesis or graceful message if foundation not ready /
    no relevant passages found / API error.
    """
    # --- Guard: empty question ---
    if not question or not question.strip():
        return "No question provided — please ask something specific about Heath's research record."

    # --- Step 1: Check foundation readiness and retrieve ---
    try:
        from agent.voice_index import retrieve_similar_sentences, is_foundation_ready  # type: ignore
    except ImportError:
        return (
            "Foundation index not yet populated — run "
            "`python -m agent.jobs.build_heath_corpus` first."
        )

    try:
        ready = is_foundation_ready()
    except Exception:
        ready = False

    if not ready:
        return (
            "Foundation index not yet populated — run "
            "`python -m agent.jobs.build_heath_corpus` first."
        )

    try:
        hits = retrieve_similar_sentences(question, k=18, min_cosine=0.55)
    except Exception as exc:
        return f"Retrieval error: {exc}"

    if not hits:
        return "No passages in your record matched the question."

    # --- Step 2: Group by paper_id, pick top max_papers by max similarity ---
    from collections import defaultdict

    paper_hits: dict[str, list[dict]] = defaultdict(list)
    for h in hits:
        paper_hits[h["paper_id"]].append(h)

    # Sort papers by their best (max) similarity score descending
    ranked_papers = sorted(
        paper_hits.items(),
        key=lambda kv: max(s["similarity"] for s in kv[1]),
        reverse=True,
    )[:max_papers]

    # --- Step 3: Build passage blocks ---
    passage_blocks: list[str] = []
    for rank, (paper_id, sentences) in enumerate(ranked_papers, start=1):
        # Sort sentences within the paper by similarity descending
        top_sentences = sorted(sentences, key=lambda s: s["similarity"], reverse=True)[
            :sentences_per_paper_cap
        ]
        year = top_sentences[0].get("year", "?")
        slug = _paper_id_short(paper_id)

        # Concatenate sentence text, respecting the char cap
        combined = ""
        for sent in top_sentences:
            text = sent.get("sentence", "").strip()
            if not text:
                continue
            candidate = (combined + " " + text).strip() if combined else text
            if len(candidate) > max_chars_per_paper:
                # Include partial if we have nothing yet
                if not combined:
                    combined = candidate[:max_chars_per_paper].rstrip()
                break
            combined = candidate

        if not combined:
            continue

        passage_blocks.append(
            f"[Paper {rank} | id:{slug} | year:{year}]\n{combined}"
        )

    if not passage_blocks:
        return "No passages in your record matched the question."

    passages_text = "\n\n".join(passage_blocks)

    # --- Step 4: Build prompt ---
    user_prompt = (
        f"Question: {question.strip()}\n\n"
        f"Passages from the researcher's publications:\n\n"
        f"{passages_text}\n\n"
        "Instructions: Answer the question in approximately 200 words using ONLY "
        "the passages above. Cite each claim with the inline format (Paper N, Year) "
        "where N is the paper number shown above (e.g. Paper 1, Paper 2). "
        "Do not invent facts not present in the passages. "
        "If the passages do not contain enough information to answer, say so explicitly."
    )

    # --- Step 5: Call Sonnet 4.6 ---
    try:
        from anthropic import Anthropic

        client = Anthropic()
        response = client.messages.create(
            model=_MODEL,
            max_tokens=400,
            temperature=0.2,
            system=(
                "You are Tealc, an AI postdoc. "
                "Answer questions about the researcher's work using only the provided passages. "
                "Always cite with (Paper N, Year) inline. Never fabricate."
            ),
            messages=[{"role": "user", "content": user_prompt}],
        )
        answer = response.content[0].text.strip()
    except Exception as exc:
        return f"Sonnet call failed: {exc}"

    # --- Optional: record cost ---
    try:
        from agent.cost_tracking import record_call  # type: ignore

        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        record_call("ask_my_record", _MODEL, usage)
    except Exception:
        pass  # non-fatal: cost tracking is best-effort

    return answer
