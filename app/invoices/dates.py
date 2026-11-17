"""Invoice date parsing and repair.

Receipts often print dates in awkward formats (Japanese eras, missing years,
two-digit years, ambiguous month/day order).  We parse every plausible
candidate and pick the one closest to the date the message was received, which
is a strong prior for a freshly photographed receipt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_ERA_START = {"令和": 2019, "平成": 1989, "昭和": 1926, "大正": 1912, "明治": 1868}
_ERA_RE = re.compile(
    r"(令和|平成|昭和|大正|明治)\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"
)
_YMD_RE = re.compile(
    r"(?<!\d)(\d{4})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?"
)
_COMPACT_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(?!\d)")
_MDY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{2,4})(?!\d)")
_MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?(?!\d)")

_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


@dataclass(frozen=True, slots=True)
class DateCandidate:
    value: date
    explicit_year: bool
    ambiguous: bool


def _normalize(text: str) -> str:
    return (text or "").translate(_FULLWIDTH)


def _add(
    candidates: list[DateCandidate],
    year: int,
    month: int,
    day: int,
    *,
    explicit_year: bool,
    ambiguous: bool = False,
) -> None:
    try:
        candidates.append(DateCandidate(date(year, month, day), explicit_year, ambiguous))
    except ValueError:
        return


def date_candidates(raw: str, received: date | None) -> list[DateCandidate]:
    text = _normalize(raw)
    candidates: list[DateCandidate] = []
    if not text:
        return candidates

    for match in _ERA_RE.finditer(text):
        era, year_text, month, day = match.groups()
        era_year = 1 if year_text == "元" else int(year_text)
        _add(
            candidates,
            _ERA_START[era] + era_year - 1,
            int(month),
            int(day),
            explicit_year=True,
        )

    for match in _YMD_RE.finditer(text):
        _add(candidates, int(match.group(1)), int(match.group(2)), int(match.group(3)), explicit_year=True)

    for match in _COMPACT_RE.finditer(text):
        _add(candidates, int(match.group(1)), int(match.group(2)), int(match.group(3)), explicit_year=True)

    for match in _MDY_RE.finditer(text):
        first, second, year_text = int(match.group(1)), int(match.group(2)), int(match.group(3))
        year = year_text if year_text >= 100 else 2000 + year_text
        if first > 12 and second <= 12:
            _add(candidates, year, second, first, explicit_year=True)
        elif second > 12 and first <= 12:
            _add(candidates, year, first, second, explicit_year=True)
        else:
            _add(candidates, year, first, second, explicit_year=True, ambiguous=True)
            _add(candidates, year, second, first, explicit_year=True, ambiguous=True)

    # Month/day without a year: infer from the received date (and the year before).
    # Mask the spans already consumed above so "令和7年11月18日" does not also
    # produce a year-less 11/18 candidate.
    masked = text
    for pattern in (_ERA_RE, _YMD_RE, _MDY_RE, _COMPACT_RE):
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)

    for match in _MD_RE.finditer(masked):
        month, day = int(match.group(1)), int(match.group(2))
        base_year = received.year if received else date.today().year
        _add(candidates, base_year, month, day, explicit_year=False, ambiguous=True)
        _add(candidates, base_year - 1, month, day, explicit_year=False, ambiguous=True)

    # De-duplicate, keeping the strongest metadata for each date.
    merged: dict[date, DateCandidate] = {}
    for candidate in candidates:
        current = merged.get(candidate.value)
        if current is None:
            merged[candidate.value] = candidate
        else:
            merged[candidate.value] = DateCandidate(
                candidate.value,
                current.explicit_year or candidate.explicit_year,
                current.ambiguous and candidate.ambiguous,
            )
    return list(merged.values())


def _score(candidate: DateCandidate, received: date) -> tuple[int, int, int, int]:
    delta_days = (received - candidate.value).days
    future = delta_days < -7
    too_old = delta_days > 365 * 5
    return (
        1 if future else 0,
        1 if too_old else 0,
        abs(delta_days),
        0 if candidate.explicit_year else 1,
    )


def resolve_invoice_date(
    raw: object, received: date | None = None
) -> tuple[date | None, bool]:
    """Return ``(date, adjusted)``.

    ``adjusted`` means the date had to be inferred (missing year), was ambiguous,
    or fell back to the received date, so the record should be flagged for review.
    """
    if isinstance(raw, datetime):
        return raw.date(), False
    if isinstance(raw, date):
        return raw, False

    text = str(raw or "").strip()
    candidates = date_candidates(text, received)
    if not candidates:
        if received is not None:
            return received, True
        return None, False

    if received is None:
        candidates.sort(key=lambda item: item.value, reverse=True)
        return candidates[0].value, not candidates[0].explicit_year

    best = min(candidates, key=lambda item: _score(item, received))

    # If every reading is in the future, retry the same month/day with the
    # received year and the year before (common with a wrong printed year).
    if best.value > received + timedelta(days=7):
        for year in (received.year, received.year - 1):
            _add(
                candidates,
                year,
                best.value.month,
                best.value.day,
                explicit_year=False,
                ambiguous=True,
            )
        best = min(candidates, key=lambda item: _score(item, received))

    adjusted = (
        not best.explicit_year
        or best.ambiguous
        or best.value > received + timedelta(days=7)
        or (received - best.value).days > 365 * 5
    )
    return best.value, adjusted
