# summarizer.py — Extracts key sentences from 8-K filing text
# Uses a simple approach: find sentences that contain the matched keywords
# and prioritize ones near the top of the document (where the key info usually is)

import re


def _trim_sentence(sentence, max_len=150):
    """Shorten a long sentence by cutting at the last natural break point."""
    if len(sentence) <= max_len:
        return sentence
    # Cut at last comma, semicolon, or dash before the limit
    truncated = sentence[:max_len]
    for sep in [", ", "; ", " — ", " - "]:
        pos = truncated.rfind(sep)
        if pos > max_len // 2:  # Only cut if we keep at least half
            return truncated[:pos] + "."
    # No good break point — cut at last space
    pos = truncated.rfind(" ")
    if pos > 0:
        return truncated[:pos] + "..."
    return truncated + "..."


# Phrases that show up in SEC filings but aren't useful for summaries
_BOILERPLATE_SIGNALS = [
    "pursuant to", "incorporated herein by reference", "item 5.02",
    "item 1.01", "item 8.01", "item 1.02", "form 8-k", "current report",
    "check the appropriate box", "registrant's telephone", "commission file",
    "date of report", "securities and exchange", "hereby incorporated",
    "filed herewith", "exhibit", "signature page", "forward-looking statements",
    "safe harbor", "private securities litigation",
]

# Patterns that indicate a sentence has a person's name (useful for summaries)
_NAME_PREFIXES = re.compile(r'\b(mr|ms|mrs|dr|miss)\.\s+[A-Z]', re.IGNORECASE)


def extract_summary(text, matched_keywords=None, max_sentences=2):
    """Pull the most relevant sentences from a filing's text.

    Strategy:
    1. Split text into sentences
    2. Score each sentence: boost keywords/names/roles, penalize boilerplate
    3. Return the top 2 sentences, trimmed for readability

    Args:
        text: Plain text of the filing
        matched_keywords: List of keywords that triggered this filing's match
        max_sentences: How many sentences to include (default 2)

    Returns:
        String with the extracted summary sentences
    """
    if not text:
        return ""

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)

    # Filter out fragments and data dumps
    sentences = [s.strip() for s in sentences if 20 < len(s.strip()) < 500]

    if not sentences:
        return text[:300] + "..." if len(text) > 300 else text

    if not matched_keywords:
        return " ".join(_trim_sentence(s) for s in sentences[:max_sentences])

    # Score each sentence
    scored_sentences = []
    keywords_lower = [kw.lower() for kw in matched_keywords]

    for i, sentence in enumerate(sentences):
        sentence_lower = sentence.lower()
        score = 0

        # +2 points per keyword found in this sentence
        for kw in keywords_lower:
            if kw in sentence_lower:
                score += 2

        # Bonus for early position (first 20% of document)
        position_ratio = i / max(len(sentences), 1)
        if position_ratio < 0.2:
            score += 1

        # Bonus for sentences mentioning executive roles
        role_terms = ["ceo", "cfo", "coo", "president", "director", "officer",
                      "chairman", "board", "chief executive", "chief financial"]
        for term in role_terms:
            if term in sentence_lower:
                score += 1
                break

        # Bonus for sentences with a person's name (Mr./Ms./Dr. + capital letter)
        if _NAME_PREFIXES.search(sentence):
            score += 2

        # Penalty for boilerplate-heavy sentences — subtract 1 per boilerplate phrase
        boilerplate_count = sum(1 for bp in _BOILERPLATE_SIGNALS if bp in sentence_lower)
        score -= boilerplate_count

        if score > 0:
            scored_sentences.append((score, i, sentence))

    if not scored_sentences:
        return " ".join(_trim_sentence(s) for s in sentences[:max_sentences])

    # Pick the highest-scoring sentences
    scored_sentences.sort(key=lambda x: (-x[0], x[1]))

    # Take top sentences, return in original document order
    top_sentences = scored_sentences[:max_sentences]
    top_sentences.sort(key=lambda x: x[1])

    # Trim long sentences and join
    summary = " ".join(_trim_sentence(s[2]) for s in top_sentences)
    return summary
