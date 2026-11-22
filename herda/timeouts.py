"""Validation helpers for timeout values."""

from __future__ import annotations

import math

from .errors import ValidationError


def validate_timeout(
    name: str,
    value: float | None,
    *,
    allow_none: bool = True,
) -> float | None:
    """Return a finite positive float, or ``None`` when allowed.

    ``NaN`` and ``inf`` are rejected deliberately: they would otherwise turn
    deadline checks into permanent waits or never-ending runs.
    """

    if value is None:
        if allow_none:
            return None
        raise ValidationError(
            "invalid_timeout", f"{name} must be a positive number"
        )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationError(
            "invalid_timeout",
            f"{name} must be a positive finite number or None",
        )
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValidationError(
            "invalid_timeout",
            f"{name} must be > 0 and finite or None",
        )
    return number
