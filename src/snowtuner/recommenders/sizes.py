"""Snowflake warehouse size ladder + parsing helpers.

Snowflake accepts several aliases for each size and returns yet another form
from ``SHOW WAREHOUSES``.  This module gives us one canonical form to work
with internally and the right rendering for ``ALTER WAREHOUSE`` SQL.
"""
from __future__ import annotations

# Canonical size ladder (low → high).  These strings are what we use in
# `AlterWarehouse` actions; Snowflake accepts them in ALTER statements.
SIZES: list[str] = [
    "XSMALL",
    "SMALL",
    "MEDIUM",
    "LARGE",
    "XLARGE",
    "X2LARGE",
    "X3LARGE",
    "X4LARGE",
    "X5LARGE",
    "X6LARGE",
]

# Approximate per-server memory in GB at each size, summed across servers.
# Snowflake doesn't publish this officially; values are community-observed
# rough orders of magnitude.  Used by SpillAwareRightSizer.
APPROX_MEMORY_GB: dict[str, float] = {
    "XSMALL":   16,
    "SMALL":    32,
    "MEDIUM":   64,
    "LARGE":    128,
    "XLARGE":   256,
    "X2LARGE":  512,
    "X3LARGE":  1024,
    "X4LARGE":  2048,
    "X5LARGE":  4096,
    "X6LARGE":  8192,
}

# Credits per hour at each size (officially published by Snowflake).
CREDITS_PER_HOUR: dict[str, float] = {
    "XSMALL":   1,
    "SMALL":    2,
    "MEDIUM":   4,
    "LARGE":    8,
    "XLARGE":   16,
    "X2LARGE":  32,
    "X3LARGE":  64,
    "X4LARGE":  128,
    "X5LARGE":  256,
    "X6LARGE":  512,
}


_ALIASES: dict[str, str] = {
    # X-Small variants
    "XSMALL": "XSMALL", "XS": "XSMALL", "X-SMALL": "XSMALL",
    # Small / Medium / Large / X-Large
    "SMALL": "SMALL", "S": "SMALL",
    "MEDIUM": "MEDIUM", "M": "MEDIUM",
    "LARGE": "LARGE", "L": "LARGE",
    "XLARGE": "XLARGE", "XL": "XLARGE", "X-LARGE": "XLARGE",
    # 2X..6X variants
    "X2LARGE": "X2LARGE", "2XLARGE": "X2LARGE", "XXLARGE": "X2LARGE",
    "2X-LARGE": "X2LARGE",
    "X3LARGE": "X3LARGE", "3XLARGE": "X3LARGE", "XXXLARGE": "X3LARGE",
    "3X-LARGE": "X3LARGE",
    "X4LARGE": "X4LARGE", "4XLARGE": "X4LARGE", "4X-LARGE": "X4LARGE",
    "X5LARGE": "X5LARGE", "5XLARGE": "X5LARGE", "5X-LARGE": "X5LARGE",
    "X6LARGE": "X6LARGE", "6XLARGE": "X6LARGE", "6X-LARGE": "X6LARGE",
}


def normalize(raw: str | None) -> str | None:
    """Map any reasonable spelling to a canonical SIZES entry, or None."""
    if not raw:
        return None
    cleaned = raw.upper().replace(" ", "")
    # Try with hyphens preserved, then without (covers "X-LARGE" and "XLARGE").
    if cleaned in _ALIASES:
        return _ALIASES[cleaned]
    no_hyphens = cleaned.replace("-", "")
    return _ALIASES.get(no_hyphens)


def step(current: str, delta: int) -> str | None:
    """Return the size delta steps away from current, or None if out of range.

    delta=+1 → next-larger size.  delta=-1 → next-smaller size.
    """
    canonical = normalize(current)
    if canonical is None:
        return None
    idx = SIZES.index(canonical)
    new_idx = idx + delta
    if 0 <= new_idx < len(SIZES):
        return SIZES[new_idx]
    return None


def credit_rate(size: str) -> float:
    """Credits/hour for a size.  Falls back to 1.0 (XSMALL) if unknown."""
    canonical = normalize(size)
    if canonical is None:
        return 1.0
    return CREDITS_PER_HOUR[canonical]


def memory_gb(size: str) -> float:
    """Approximate aggregate memory in GB.  Falls back to 16 (XSMALL) if unknown."""
    canonical = normalize(size)
    if canonical is None:
        return 16.0
    return APPROX_MEMORY_GB[canonical]
