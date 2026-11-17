"""MusicRecs AI: the orchestrator.

Pipeline for every request:
  1. Guardrail  - reject empty, too-long, or prompt-injection input
  2. Parse      - plain English -> validated taste profile (Gemini, or keyword fallback)
  3. Retrieve   - score every song in the catalog and keep the top k (the RAG "retrieval" step)
  4. Generate   - Gemini writes the answer using ONLY the retrieved songs
  5. Verify     - check the answer for songs that were not retrieved (hallucinations);
                  retry once, then fall back to a safe template answer
  6. Score      - attach a confidence score; low confidence asks the human to refine
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.recommender import DEFAULT_WEIGHTS, recommend_songs
from src.request_parser import check_request, parse_request

log = logging.getLogger("musicrecs.assistant")

LOW_CONFIDENCE = 0.5


@dataclass
class Result:
    """Everything the system produced for one request (used by the CLI, tests, and evaluation)."""

    request: str
    status: str  # "ok" | "blocked" | "needs_clarification"
    answer: str = ""
    profile: Dict = field(default_factory=dict)
    parser: str = ""
    recommendations: List[Tuple[Dict, float, str]] = field(default_factory=list)
    generator: str = ""  # "llm" | "template"
    verified: bool = False
    retries: int = 0
    confidence: float = 0.0
    warnings: List[str] = field(default_factory=list)


GENERATE_PROMPT = """You are MusicRecs, a friendly music recommendation assistant.

The user asked: "{request}"

Here are the ONLY songs you may recommend. They were retrieved from our catalog and
are already ranked from best to worst match, with the reasons each one matched:
{context}

Write a short reply (3-6 sentences) that recommends these songs in this order and
explains in plain language why they fit the request, using the match reasons.
Rules:
- Mention songs ONLY from the list above, using their exact titles.
- Do not invent songs, artists, or facts that are not in the list.
- If a song is only a weak match, say so honestly.
{extra}"""

STRICT_RETRY = "- IMPORTANT: your last answer mentioned songs that are not in the list. Use only the listed titles."


def max_possible_score(profile: Dict, weights: Optional[Dict] = None) -> float:
    """Highest score any song could get for this profile (used to normalize confidence)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    total = 0.0
    total += w["genre"] if "genre" in profile else 0
    total += w["mood"] if "mood" in profile else 0
    total += w["energy"] if "energy" in profile else 0
    total += w["acoustic"] if "likes_acoustic" in profile else 0
    return total


def confidence_score(profile: Dict, recommendations: List) -> float:
    """0-1 score = (how much of the request we understood) x (how well the top song fits).

    Multiplying means confidence is only high when BOTH are high: a vague request
    or a request nothing in the catalog fits will both score low.
    """
    coverage = sum(k in profile for k in ("genre", "mood", "energy")) / 3
    best = recommendations[0][1] if recommendations else 0.0
    possible = max_possible_score(profile)
    match = best / possible if possible else 0.0
    return round(coverage * match, 2)


def verify_answer(answer: str, retrieved: List[Dict], catalog: List[Dict]) -> Tuple[bool, List[str]]:
    """Return (ok, problems). Flags catalog songs that weren't retrieved, and answers that name no retrieved song."""
    text = answer.lower()
    retrieved_titles = {s["title"].lower() for s in retrieved}
    problems = []
    for song in catalog:
        title = song["title"].lower()
        if title not in retrieved_titles and re.search(r"(?<![a-z])" + re.escape(title) + r"(?![a-z])", text):
            problems.append(f"mentions non-retrieved song '{song['title']}'")
    if not any(t in text for t in retrieved_titles):
        problems.append("does not mention any retrieved song")
    return (not problems), problems


def template_answer(request: str, recommendations: List[Tuple[Dict, float, str]]) -> str:
    """Safe, deterministic answer built only from retrieved songs (used offline or after failed verification)."""
    lines = [f'Here are my top {len(recommendations)} picks for "{request}":']
    for rank, (song, score, reasons) in enumerate(recommendations, 1):
        lines.append(f"  {rank}. {song['title']} by {song['artist']} ({song['genre']}, {song['mood']}) "
                     f"- score {score:.2f}: {reasons}")
    return "\n".join(lines)


class MusicAssistant:
    """Runs the full guardrail -> parse -> retrieve -> generate -> verify pipeline."""

    def __init__(self, songs: List[Dict], llm=None, k: int = 5):
        """Store the catalog, the (optional) LLM client, and how many songs to return."""
        self.songs = songs
        self.llm = llm
        self.k = k
        self.genres = sorted({s["genre"] for s in songs})
        self.moods = sorted({s["mood"] for s in songs})

    def _generate(self, request: str, recs: List[Tuple[Dict, float, str]], result: Result) -> None:
        """Fill result.answer using the LLM with verification + one retry, or the template fallback."""
        retrieved = [song for song, _, _ in recs]
        if self.llm is not None:
            context = "\n".join(
                f"{i}. \"{s['title']}\" by {s['artist']} ({s['genre']}, {s['mood']}, energy {s['energy']}) "
                f"- score {score:.2f}; reasons: {reasons}"
                for i, (s, score, reasons) in enumerate(recs, 1)
            )
            extra = ""
            for attempt in range(2):  # first try + one retry
                try:
                    answer = self.llm.generate(GENERATE_PROMPT.format(request=request, context=context, extra=extra))
                except Exception as exc:
                    log.error("Generation failed: %s", exc)
                    result.warnings.append("AI generation failed; showed the plain ranked list instead.")
                    break
                ok, problems = verify_answer(answer, retrieved, self.songs)
                if ok:
                    result.answer, result.generator, result.verified = answer, "llm", True
                    log.info("LLM answer verified on attempt %d", attempt + 1)
                    return
                log.warning("Verification failed (attempt %d): %s", attempt + 1, problems)
                result.retries = attempt + 1
                extra = STRICT_RETRY
            else:
                result.warnings.append("AI answer failed verification twice; showed the plain ranked list instead.")
        result.answer = template_answer(request, recs)
        result.generator = "template"
        result.verified = True  # built only from retrieved songs, so verified by construction

    def recommend(self, request: str) -> Result:
        """Answer one plain-English music request."""
        log.info("REQUEST %r", request)
        ok, reason = check_request(request)
        if not ok:
            log.info("BLOCKED: %s", reason)
            return Result(request=request, status="blocked", answer=reason)

        profile, parser, is_music = parse_request(request, self.genres, self.moods, self.llm)
        if not is_music:
            log.info("BLOCKED: not a music request")
            return Result(request=request, status="blocked", parser=parser,
                          answer="I can only help with music recommendations.")

        result = Result(request=request, status="ok", profile=profile, parser=parser)
        if not any(k in profile for k in ("genre", "mood", "energy")):
            result.status = "needs_clarification"
            result.answer = ("I couldn't tell what you're in the mood for. Try naming a genre "
                             "(e.g. lofi, rock, jazz), a mood (happy, sad, chill), or an activity "
                             "(studying, workout, party).")
            log.info("NEEDS CLARIFICATION: %s", profile)
            return result

        # Retrieval guardrail: never pad the list with songs that matched nothing (score 0).
        result.recommendations = [r for r in recommend_songs(profile, self.songs, self.k) if r[1] > 0]
        result.confidence = confidence_score(profile, result.recommendations)
        if result.confidence < LOW_CONFIDENCE:
            result.warnings.append(
                f"Low confidence ({result.confidence:.2f}): these are loose matches. "
                "Try adding a genre or mood to get better picks."
            )
        self._generate(request, result.recommendations, result)
        log.info("RESULT parser=%s generator=%s confidence=%.2f top=%r", result.parser, result.generator,
                 result.confidence, result.recommendations[0][0]["title"])
        return result
