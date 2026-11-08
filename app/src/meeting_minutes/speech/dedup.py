"""Boundary de-duplication for forced cuts.

A forced cut repeats its last `forced_overlap_ms` at the start of the next chunk, so
the next text may begin with words already committed. Only that boundary is
examined: the longest run of leading words of the new text that equals the trailing
words of the previous text of the same source and epoch is removed. Matching uses
normalized tokens; the displayed text keeps its original spelling and script. If no
alignment is found, both texts are kept and the boundary is flagged; repeated phrases
elsewhere, or across sources, are never touched.
"""

from __future__ import annotations

import unicodedata

_TATWEEL = "ـ"
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                              "ى": "ي", "ة": "ه"})


def normalize_token(token: str) -> str:
    """Matching key only: NFKC, casefold, no marks/tatweel/punctuation, folded alef/ya/ta marbuta."""
    text = unicodedata.normalize("NFKC", token).casefold().replace(_TATWEEL, "")
    kept = [c for c in text if not unicodedata.category(c).startswith(("M", "P", "S"))]
    return "".join(kept).translate(_ARABIC_FOLD)


def trim_overlap(previous: str, text: str, *, max_tokens: int) -> tuple[str, int, bool]:
    """Return (text without the repeated leading words, words removed, ambiguous)."""
    prev = [k for k in (normalize_token(t) for t in previous.split()) if k]
    words = text.split()
    keyed = [(i, k) for i, t in enumerate(words) if (k := normalize_token(t))]
    if not prev or not keyed:
        return text, 0, False
    limit = min(max_tokens, len(prev), len(keyed))
    for n in range(limit, 0, -1):
        if prev[-n:] == [k for _, k in keyed[:n]]:
            cut = keyed[n - 1][0] + 1
            return " ".join(words[cut:]), n, False
    return text, 0, True
