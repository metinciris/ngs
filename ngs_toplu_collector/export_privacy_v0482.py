"""Per-export identifier redaction with bounded preparation and text caching.

Only local identity tokens are retained by this short-lived object. Structured
private-field removal remains the responsibility of the export serializer.
No clinical values are inferred, truncated, or reclassified here.
"""
from __future__ import annotations

from collections import OrderedDict
import re
from typing import Iterable, Iterator


NAME_REPLACEMENT = '[AD GİZLENDİ]'
NATIONAL_ID_REPLACEMENT = '[TC GİZLENDİ]'
EMAIL_REPLACEMENT = '[EPOSTA GİZLENDİ]'
_GENERIC_NATIONAL_ID = re.compile(r'(?<!\d)\d{11}(?!\d)')


def _word_character(char: str) -> bool:
    # Python's Unicode \w character class, without invoking a regex per character.
    return char == '_' or char.isalnum()


def _email_spans(text: str) -> Iterator[tuple[int, int]]:
    r"""Equivalent spans to [\w.+-]+@[\w.-]+\.[A-Za-z]{2,}, without rescans.

    An unanchored leading \w+ repeatedly retries every suffix of a long
    identifier-free token. Starting at actual @ characters avoids that quadratic
    failure mode. Each adjacent token is visited at most twice.
    """
    cursor = 0
    search_from = 0
    length = len(text)
    while True:
        at = text.find('@', search_from)
        if at < 0:
            return
        search_from = at + 1
        start = at
        while start > cursor:
            char = text[start - 1]
            if not (_word_character(char) or char in '.+-'):
                break
            start -= 1
        if start == at:
            continue
        domain_start = at + 1
        domain_end = domain_start
        while domain_end < length:
            char = text[domain_end]
            if not (_word_character(char) or char in '.-'):
                break
            domain_end += 1
        # The original greedy domain chooses the last dot followed by at least
        # two ASCII letters, with at least one domain character before that dot.
        limit = domain_end
        while True:
            dot = text.rfind('.', domain_start + 1, limit)
            if dot < 0:
                break
            end = dot + 1
            while end < domain_end and ('A' <= text[end] <= 'Z' or 'a' <= text[end] <= 'z'):
                end += 1
            if end - dot - 1 >= 2:
                yield start, end
                cursor = end
                search_from = end
                break
            limit = dot


def has_email_address(value: str) -> bool:
    return next(_email_spans(str(value or '')), None) is not None


def redact_email_addresses(value: str) -> str:
    text = str(value or '')
    pieces = []
    cursor = 0
    for start, end in _email_spans(text):
        pieces.append(text[cursor:start])
        pieces.append(EMAIL_REPLACEMENT)
        cursor = end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return ''.join(pieces)


def _compile_literals(values: Iterable[str], flags: int = 0):
    # One pattern per identifier kind, not a different pattern compilation for
    # every identifier at every JSON key/value. Longest alternatives win at the
    # same position, retaining full names ahead of their shorter aliases.
    values = sorted({str(value) for value in values if value}, key=lambda value: (-len(value), value))
    return re.compile('|'.join(re.escape(value) for value in values), flags) if values else None


class ExportPrivacyRedactor:
    """Prepare once for COMPACT/FULL and raw-source checks of one export.

    Literal matching retains the previous Unicode IGNORECASE name behavior,
    including Turkish I variants, punctuation, titles and supplied whitespace.
    Cache size and cached-string length are capped; long clinical/QC strings
    still pass through in full. There is no process-wide patient-token cache.
    """

    def __init__(self, names: Iterable[str] = (), national_ids: Iterable[str] = (),
                 *, cache_size: int = 2048, max_cached_chars: int = 512):
        names = tuple(str(value) for value in names if value)
        self._name_pattern = _compile_literals(names, re.IGNORECASE)
        # Raw-source omission historically used str.casefold(), which includes
        # multi-character folds (e.g. sharp S / ligatures) that re.IGNORECASE
        # deliberately does not. Keep that stronger source gate as well.
        self._source_name_lookup = {value.casefold(): value for value in names}
        self._source_name_pattern = _compile_literals(self._source_name_lookup)
        self._id_pattern = _compile_literals(national_ids)
        self._cache_size = max(0, int(cache_size))
        self._max_cached_chars = max(0, int(max_cached_chars))
        self._cache: OrderedDict[str, str] = OrderedDict()

    def redact(self, value: str) -> str:
        text = str(value or '')
        cacheable = self._cache_size > 0 and len(text) <= self._max_cached_chars
        if cacheable and text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        result = text
        if self._id_pattern is not None:
            result = self._id_pattern.sub(NATIONAL_ID_REPLACEMENT, result)
        if self._name_pattern is not None:
            result = self._name_pattern.sub(NAME_REPLACEMENT, result)
        result = redact_email_addresses(result)
        result = _GENERIC_NATIONAL_ID.sub(NATIONAL_ID_REPLACEMENT, result)
        if cacheable:
            self._cache[text] = result
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result

    def find_name_hits(self, value: str) -> tuple[str, ...]:
        """Known names for source omission; never export the returned tokens raw."""
        if self._name_pattern is None:
            return ()
        text = str(value or '')
        hits = {}
        for match in self._source_name_pattern.finditer(text.casefold()):
            hits[self._source_name_lookup[match.group(0)]] = None
        for match in self._name_pattern.finditer(text):
            hit = self._source_name_lookup.get(match.group(0).casefold(), match.group(0))
            hits[hit] = None
        return tuple(hits)

    def find_national_id_hits(self, value: str) -> tuple[str, ...]:
        if self._id_pattern is None:
            return ()
        return tuple(dict.fromkeys(match.group(0) for match in self._id_pattern.finditer(str(value or ''))))
