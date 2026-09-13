"""Extraction prompt for the OpenAI event extractor.

The corridor enum is the 3-corridor model the graph actually uses — Suez was
folded into Red Sea in Phase 1 (the Suez Canal and Bab-el-Mandeb are serial
chokepoints on the same Mediterranean route). The prompt says so explicitly, and
extractor.CORRIDOR_ALIASES remaps a stray "Suez" answer as a second line of
defence.
"""

CORRIDOR_VALUES = ("Hormuz", "Red Sea", "Cape", "None")
EVENT_TYPE_VALUES = ("sanction", "military", "shipping", "policy", "other")

_ARTICLE_PLACEHOLDER = "{article_text}"

EXTRACTION_PROMPT = """You are an expert analyst extracting geopolitical risk information
from news articles related to oil supply chains.

Extract information and return ONLY a valid JSON object.
No explanation, no preamble, no markdown — just the JSON.

Required fields:
{
    "corridor": "Hormuz" | "Red Sea" | "Cape" | "None",
    "actor": "country or entity name as string",
    "event_type": "sanction" | "military" | "shipping" | "policy" | "other",
    "severity": integer 1-5 where 1=minor 5=critical,
    "confidence": float 0.0-1.0 how confident you are in this extraction,
    "is_relevant": true if article relates to oil supply chain risk else false
}

Corridor definitions:
- Hormuz: Strait of Hormuz, Persian Gulf, Gulf of Oman
- Red Sea: Red Sea, Bab-el-Mandeb, Gulf of Aden, Houthi attacks on shipping,
  AND the Suez Canal / Egypt — these are one corridor in this model, so never
  answer "Suez"
- Cape: Cape of Good Hope, rerouting via southern Africa
- None: not specific to any of the three corridors above

Severity guidance:
1 = routine commentary or forecasting
2 = policy signalling, minor incident, no flow impact
3 = a concrete incident affecting some vessels or volumes
4 = sustained disruption, sanctions enforcement, or military action on the route
5 = corridor closure or an event halting a large share of traffic

The article text may contain a "matched_corridor_query:" line. That records which
search query surfaced the article — treat it as a weak hint only, and judge the
corridor from the article's own content.

If is_relevant is false, still return all fields but set severity=1 confidence=0.1

Article:
{article_text}
"""


def build_prompt(article_text):
    """Insert the article into the template.

    Uses str.replace rather than str.format because the template contains the
    literal JSON braces of the required-fields block.
    """
    return EXTRACTION_PROMPT.replace(_ARTICLE_PLACEHOLDER, article_text or "")
