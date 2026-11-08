"""Search normalization and literal FTS queries.

The index holds a normalized copy; the original text is stored separately and never
changed by normalization. User input is always matched literally: every token is
quoted, so FTS operators typed by the user are plain words.
"""

from __future__ import annotations

import re
import unicodedata

MAX_QUERY_CHARS = 500
MAX_PAGE = 100

_TATWEEL = "ـ"
# Arabic harakat, tanwin, shadda, sukun, superscript alef and Quranic marks.
_ARABIC_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭ]")
_ALEF_VARIANTS = str.maketrans({"آ": "ا", "أ": "ا", "إ": "ا", "ٱ": "ا"})
# Arabic-Indic and Extended (Persian) digits -> ASCII.
_DIGITS = str.maketrans({**{chr(0x0660 + i): str(i) for i in range(10)},
                         **{chr(0x06F0 + i): str(i) for i in range(10)}})
_TOKEN = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    """NFC, casefold, drop tatweel and optional Arabic marks, unify alef forms and
    digits. Ta marbuta/ha and ya/alef maqsura are deliberately kept distinct."""
    text = unicodedata.normalize("NFC", text).casefold().replace(_TATWEEL, "")
    text = _ARABIC_MARKS.sub("", text)
    return text.translate(_ALEF_VARIANTS).translate(_DIGITS)


class QueryError(ValueError):
    pass


def literal_query(user_text: str) -> str | None:
    """An FTS5 MATCH expression that finds the user's words literally (all of them,
    in any order). Returns None when there is nothing searchable."""
    if len(user_text) > MAX_QUERY_CHARS:
        raise QueryError(f"search text is limited to {MAX_QUERY_CHARS} characters")
    tokens = _TOKEN.findall(normalize(user_text))
    if not tokens:
        return None
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)
