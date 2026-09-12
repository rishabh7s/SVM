"""
Renders a fact's value for display.

A fact's value lives in value_text OR value_numeric+unit, never both. Every
display path has to put them back together; the one that forgot printed
"budget: None". Keep this the only place that does it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# substring match, so 'budget reduction' and 'annual_cost' both hit
_CURRENCY_ATTRIBUTE_HINTS = ("budget", "cost", "price", "spend", "salary", "revenue", "value", "fee")

# anything not listed renders as a suffix ("1,200 GBP") rather than guessed
_CURRENCY_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥"}

# "15%", not "15 percent"
_SUFFIX_UNITS = {"percent": "%", "%": "%", "pct": "%"}

EMPTY_VALUE_PLACEHOLDER = "—"  # never the string "None"


def format_number(value: float) -> str:
    """40000.0 -> '40,000', 0.001 -> '0.001'."""
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    # trim trailing zeros without falling into scientific notation
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def format_value(
    value_text: Optional[str] = None,
    value_numeric: Optional[float] = None,
    unit: Optional[str] = None,
    attribute: Optional[str] = None,
) -> str:
    """value_text wins (it's what the user actually said), then the number,
    then a placeholder. Never returns "None"."""
    if value_text is not None and str(value_text).strip() != "":
        return str(value_text)

    if value_numeric is None:
        return EMPTY_VALUE_PLACEHOLDER

    try:
        numeric = float(value_numeric)
    except (TypeError, ValueError):
        return str(value_numeric)

    number = format_number(numeric)
    unit_key = (unit or "").strip()
    attribute_key = (attribute or "").lower()

    if unit_key.upper() in _CURRENCY_SYMBOLS:
        return f"{_CURRENCY_SYMBOLS[unit_key.upper()]}{number}"

    if unit_key.lower() in _SUFFIX_UNITS:
        return f"{number}{_SUFFIX_UNITS[unit_key.lower()]}"

    if unit_key:
        return f"{number} {unit_key}"

    # no unit: a budget of 40000 is still money, a count isn't
    if any(hint in attribute_key for hint in _CURRENCY_ATTRIBUTE_HINTS):
        return f"{_CURRENCY_SYMBOLS['USD']}{number}"

    return number


def _get(row: Mapping[str, Any], key: str) -> Any:
    """Row lookup that tolerates a query which didn't SELECT that column."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def format_fact_row(row: Mapping[str, Any]) -> str:
    """format_value for a row straight out of declarative_facts."""
    return format_value(
        value_text=_get(row, "value_text"),
        value_numeric=_get(row, "value_numeric"),
        unit=_get(row, "unit"),
        attribute=_get(row, "attribute"),
    )
