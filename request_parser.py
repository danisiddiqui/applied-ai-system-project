"""Step 1 of the pipeline: turn a plain-English request into a validated taste profile.

- With an LLM: Gemini extracts genre / mood / energy / acoustic as JSON.
- Without one (or if the LLM output is bad): a keyword parser does it.
Either way, the result goes through the same guardrails before it is used.
"""

import json
import logging
import re
from typing import Dict, Iterable, Optional, Tuple

log = logging.getLogger("musicrecs.parser")

MAX_REQUEST_CHARS = 300

# Phrases that try to hijack the assistant instead of asking for music.
INJECTION_PATTERNS = [
    r"ignore (all|any|the|previous|prior)",
    r"system prompt",
    r"you are now",
    r"disregard (the|your|all)",
    r"reveal (your|the) (instructions|prompt)",
]

# Keyword parser vocabulary --------------------------------------------------

GENRE_SYNONYMS = {
    "lofi": ["lofi", "lo-fi", "lo fi"],
    "pop": ["pop"],
    "indie pop": ["indie pop", "indie"],
    "rock": ["rock"],
    "metal": ["metal", "heavy metal"],
    "edm": ["edm", "electronic", "dance music", "house", "techno"],
    "synthwave": ["synthwave", "synth", "retro", "80s"],
    "hip hop": ["hip hop", "hip-hop", "rap"],
    "r&b": ["r&b", "rnb", "r and b"],
    "latin": ["latin", "reggaeton", "salsa"],
    "reggae": ["reggae"],
    "jazz": ["jazz"],
    "blues": ["blues"],
    "country": ["country"],
    "folk": ["folk"],
    "ambient": ["ambient"],
    "classical": ["classical", "piano", "orchestra"],
}

MOOD_SYNONYMS = {
    "chill": ["chill", "laid back", "laid-back", "mellow"],
    "happy": ["happy", "upbeat", "cheerful", "feel good", "feel-good", "sunny"],
    "intense": ["intense", "aggressive", "hype", "pump", "pumped", "gym", "workout", "lifting"],
    "relaxed": ["relaxed", "relaxing", "unwind", "easy"],
    "romantic": ["romantic", "love", "date night", "date"],
    "sad": ["sad", "heartbreak", "heartbroken", "crying", "melancholy"],
    "moody": ["moody", "dark", "night drive"],
    "focused": ["focus", "focused", "concentrate", "concentration"],
    "confident": ["confident", "swagger", "boss"],
    "angry": ["angry", "rage", "mad"],
    "peaceful": ["peaceful", "calm", "sleep", "meditate", "meditation"],
    "nostalgic": ["nostalgic", "nostalgia", "throwback", "memories"],
    "energetic": ["energetic", "party", "dance", "dancing"],
}

# Activities imply an energy level even when no genre or mood is named.
ENERGY_HINTS = [
    (["low energy", "sleep", "meditat", "calm", "peaceful", "quiet", "soft"], 0.2),
    (["sad", "heartbr", "rain", "cry", "melanchol"], 0.3),
    (["study", "studying", "focus", "homework", "reading", "chill", "relax", "mellow"], 0.35),
    (["dinner", "date", "cooking", "road trip", "drive"], 0.55),
    (["happy", "upbeat", "sunny", "feel good"], 0.75),
    (["high energy", "workout", "gym", "run", "running", "lifting", "party", "hype", "pump", "intense", "dance"], 0.9),
]

ACOUSTIC_YES = ["acoustic", "unplugged", "guitar", "piano", "no electronic"]
ACOUSTIC_NO = ["electronic", "edm", "synth", "bass", "no acoustic"]


def _word_in(text: str, phrase: str) -> bool:
    """True if `phrase` appears in `text` as whole word(s)."""
    # Hyphens count as part of a word, so "k-pop" does NOT match "pop" (bug found by src/evaluate.py).
    return re.search(r"(?<![a-z-])" + re.escape(phrase) + r"(?![a-z-])", text) is not None


# Guardrails -----------------------------------------------------------------

def check_request(text: Optional[str]) -> Tuple[bool, str]:
    """Input guardrail: reject empty, overly long, or prompt-injection requests."""
    if text is None or not text.strip():
        return False, "Please type what kind of music you want."
    if len(text) > MAX_REQUEST_CHARS:
        return False, f"Please keep requests under {MAX_REQUEST_CHARS} characters."
    lowered = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            log.warning("Blocked possible prompt injection: %r", text)
            return False, "I can only help with music recommendations."
    return True, ""


def validate_profile(raw: Dict, genres: Iterable[str], moods: Iterable[str]) -> Dict:
    """Output guardrail: keep only known genres/moods, clamp energy to 0-1, coerce acoustic to bool."""
    genres = {g.lower() for g in genres}
    moods = {m.lower() for m in moods}
    profile: Dict = {}

    genre = str(raw.get("genre") or "").strip().lower()
    if genre in genres:
        profile["genre"] = genre
    elif genre and genre not in ("none", "null", "any"):
        log.info("Dropped unknown genre %r (not in catalog)", genre)

    mood = str(raw.get("mood") or "").strip().lower()
    if mood in moods:
        profile["mood"] = mood
    elif mood and mood not in ("none", "null", "any"):
        log.info("Dropped unknown mood %r (not in catalog)", mood)

    energy = raw.get("energy")
    if energy is not None:
        try:
            profile["energy"] = round(min(1.0, max(0.0, float(energy))), 2)
        except (TypeError, ValueError):
            log.info("Dropped invalid energy %r", energy)

    acoustic = raw.get("likes_acoustic")
    if isinstance(acoustic, bool):
        profile["likes_acoustic"] = acoustic
    elif isinstance(acoustic, str) and acoustic.lower() in ("true", "false"):
        profile["likes_acoustic"] = acoustic.lower() == "true"

    return profile


# Parsers --------------------------------------------------------------------

def keyword_parse(text: str) -> Dict:
    """Offline parser: find genre, mood, energy, and acoustic preference from keywords."""
    t = text.lower()
    raw: Dict = {}

    # Longest synonyms first so "indie pop" wins over "pop".
    genre_hits = sorted(
        ((syn, g) for g, syns in GENRE_SYNONYMS.items() for syn in syns if _word_in(t, syn)),
        key=lambda pair: -len(pair[0]),
    )
    if genre_hits:
        raw["genre"] = genre_hits[0][1]

    for mood, syns in MOOD_SYNONYMS.items():
        if any(_word_in(t, s) for s in syns):
            raw["mood"] = mood
            break

    for words, level in ENERGY_HINTS:
        if any(re.search(r"(?<![a-z])" + re.escape(w), t) for w in words):
            raw["energy"] = level
            break

    if any(_word_in(t, w) for w in ACOUSTIC_YES):
        raw["likes_acoustic"] = True
    elif any(_word_in(t, w) for w in ACOUSTIC_NO):
        raw["likes_acoustic"] = False
    return raw


PARSE_PROMPT = """You convert a music request into a JSON taste profile.

Allowed genres: {genres}
Allowed moods: {moods}

Return ONLY a JSON object with these keys:
- "genre": one allowed genre, or null if the user did not imply one
- "mood": one allowed mood, or null
- "energy": a number from 0.0 (very calm) to 1.0 (very intense), or null
- "likes_acoustic": true, false, or null
- "is_music_request": false if the request is not about music, otherwise true

Request: "{request}"
JSON:"""


def llm_parse(text: str, llm, genres: Iterable[str], moods: Iterable[str]) -> Optional[Dict]:
    """Ask the LLM for a JSON profile; return None if the output is not valid JSON."""
    prompt = PARSE_PROMPT.format(genres=", ".join(sorted(genres)), moods=", ".join(sorted(moods)), request=text)
    reply = llm.generate(prompt, temperature=0.0)
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        log.warning("LLM parse returned no JSON: %r", reply[:200])
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("LLM parse returned invalid JSON: %r", reply[:200])
        return None


def parse_request(text: str, genres: Iterable[str], moods: Iterable[str], llm=None) -> Tuple[Dict, str, bool]:
    """Return (validated_profile, parser_used, is_music_request)."""
    genres, moods = list(genres), list(moods)
    if llm is not None:
        try:
            raw = llm_parse(text, llm, genres, moods)
            if raw is not None:
                is_music = raw.get("is_music_request", True) is not False
                profile = validate_profile(raw, genres, moods)
                log.info("LLM parsed %r -> %s", text, profile)
                return profile, "llm", is_music
        except Exception as exc:  # any LLM failure falls back to keywords
            log.warning("LLM parse failed (%s); using keyword parser", exc)
    profile = validate_profile(keyword_parse(text), genres, moods)
    log.info("Keyword parsed %r -> %s", text, profile)
    return profile, "keyword", True
