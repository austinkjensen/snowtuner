"""Display-layer formatting helpers shared across CLI + UI.

The recommendation pipeline computes precise floats; this module decides how
those numbers should *read* to a human.  Importantly: when |value| rounds to
zero at two decimal places, we don't want to display a misleading ``-0.00``
or ``+0.00`` — that signals "no impact" when the truth is "small impact, the
recommendation is still valid."
"""
from __future__ import annotations

# At |value| < this, we render "≈0" instead of a signed zero.  Threshold is
# half a cent so anything that would round to ±0.00 at two decimals trips it.
_NEGLIGIBLE = 0.005


def credits_delta(value: float | None) -> str:
    """Format a credit delta for human display.

    Conventions:
      None       → "—"        (impact unknown / not computed)
      |v| < .005 → "≈0"       (avoids the misleading "-0.00" / "+0.00")
      otherwise  → "+1.23" or "-1.23"  (signed, two decimals)
    """
    if value is None:
        return "—"
    if abs(value) < _NEGLIGIBLE:
        return "≈0"
    return f"{value:+.2f}"


def copy_to_clipboard(text: str) -> bool:
    """Best-effort copy to the OS clipboard.  Returns True on success.

    Uses ``pbcopy`` on macOS, ``wl-copy``/``xclip``/``xsel`` on Linux, and
    ``clip`` on Windows.  Returns False (and never raises) if no working
    backend is found — the caller should treat clipboard copy as a nice-to-have,
    not a contract.
    """
    import shutil
    import subprocess
    import sys

    candidates: list[list[str]]
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        # Wayland first (modern Linux), then X11 fallbacks.
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]

    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=text, text=True, check=True, timeout=2)
            return True
        except Exception:
            continue
    return False


def credits_savings_for_metric(value: float | None) -> tuple[str, str]:
    """Return ``(label, value_text)`` suited for a UI "metric" tile.

    Matches the convention used in the Streamlit detail panel: positive deltas
    are framed as "added cost", negative as "savings".  Negligible deltas use
    a neutral label so the tile doesn't pretend to show a precise zero.
    """
    if value is None:
        return ("Daily credit impact", "—")
    if abs(value) < _NEGLIGIBLE:
        return ("Daily credit impact", "≈0")
    if value < 0:
        return ("Daily credit savings", f"{abs(value):.2f}")
    return ("Daily credit added cost", f"{value:.2f}")
