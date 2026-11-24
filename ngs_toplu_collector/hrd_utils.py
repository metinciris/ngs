"""Strict HRD Score extraction (v0.4.73 reporting correction).

HRD-LOH / LST / TAI are component scores, NEVER substitute for HRD Score.
Invalid/ambiguous content is missing, not zero, a nearby number or a result.
"""
from __future__ import annotations
import re

_TOKEN = r"(?:N\s*/\s*A|N\.?\s*A\.?|[+-]?\d+(?:[.,]\d+)?)"
_NUM = r"[+-]?\d+(?:[.,]\d+)?"
_LABEL = r"(?:HRD\s*[-_ ]*Score|HRD\s+skor(?:u)?|HRD)"


def normalize_hrd(value, *, allow_bare=False):
    """Normalize a dedicated HRD-score field or labelled text to score / N/A / ''.

    allow_bare is ONLY appropriate for an already typed/dedicated HRD Score field.
    Never call allow_bare=True on a whole card, biomarker group or body text.
    """
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if not text:
        return ""
    if allow_bare:
        if re.fullmatch(r"(?i)(?:N\s*/\s*A|N\.?\s*A\.?)", text):
            return "N/A"
        if re.fullmatch(_NUM, text):
            return text
    # Explicit label, optional separator, then EXACT value. No crossing fields.
    # Lookahead prevents HRD-LOH, HRD-LST, HRD-AI from being read as scores.
    for match in re.finditer(
        rf"(?i)(?<![\w-])({_LABEL})(?![\w-])\s*[:=]?\s*({_TOKEN})(?![\w.,/])",
        text,
    ):
        label = match.group(1)
        value = match.group(2).strip()
        if label.upper().replace(' ', '') == 'HRD' and re.match(r'[-_]', text[match.start(2)-1:match.start(2)]):
            continue
        if re.fullmatch(r"(?i)N\s*/\s*A|N\.?\s*A\.?", value):
            return 'N/A'
        return value
    return ''


# Dedicated Runs-row extractor: do not infer HRD total from generic HRD badges.
# The sample-specific Run Detail HRD Score field is the authoritative source.
def normalize_explicit_hrd_score(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for match in re.finditer(
        rf"(?i)(?<![\w-])(?:HRD[\s_-]*Score|HRD\s+skor(?:u)?)(?![\w-])\s*[:=]?\s*({_TOKEN})(?![\w.,/])",
        text,
    ):
        return normalize_hrd(match.group(1), allow_bare=True)
    return ""
