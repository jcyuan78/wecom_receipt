"""Invoice date inference tests."""

from __future__ import annotations

from datetime import date

import pytest

from app.invoices.dates import resolve_invoice_date


@pytest.mark.parametrize(
    ("raw", "received", "expected", "adjusted"),
    [
        ("2026-11-18", date(2026, 11, 18), date(2026, 11, 18), False),
        ("2026年11月18日", date(2026, 11, 18), date(2026, 11, 18), False),
        ("令和7年11月18日", date(2026, 11, 18), date(2025, 11, 18), False),
        ("11月18日", date(2026, 11, 18), date(2026, 11, 18), True),
        ("11/18", date(2026, 1, 5), date(2025, 11, 18), True),
        ("01/02/2026", date(2026, 1, 5), date(2026, 1, 2), True),
        ("15/01/2026", date(2026, 1, 20), date(2026, 1, 15), False),
        ("2026-13-01", date(2026, 11, 18), date(2026, 11, 18), True),
        ("2027/01/15", date(2026, 11, 18), date(2026, 1, 15), True),
        ("", date(2026, 11, 18), date(2026, 11, 18), True),
        (None, date(2026, 11, 18), date(2026, 11, 18), True),
    ],
)
def test_resolve_invoice_date(raw, received, expected, adjusted):
    value, was_adjusted = resolve_invoice_date(raw, received)
    assert value == expected
    assert was_adjusted is adjusted


def test_explicit_old_invoice_is_kept():
    value, adjusted = resolve_invoice_date("2024/03/05", date(2026, 11, 18))
    assert value == date(2024, 3, 5)
    assert adjusted is False
