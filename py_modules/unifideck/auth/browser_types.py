"""auth/browser_types.py — Shared result type for OAuth captures.

Extracted from ``auth/browser.py`` in lot 13a (file-cap split):
the OAuth monitor module grew past the 550-line volumetry gate
after the cognitive-complexity refactor. Splitting the pure
data-shape into its own module both respects the gate and
makes the type independently importable for tests and callers
that only need the return shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INNER_TEXT_EXPRESSION = "document.body?.innerText || ''"


@dataclass(frozen=True)
class ContentCapture:
    """Read the auth code out of a page instead of its URL.

    When a tab's URL contains ``trigger_url``, the monitor evaluates
    ``expression`` in that page over CDP and applies ``regex`` to the
    string it returns; the first capture group is the code.

    ``expression`` defaults to the visible text. It exists for pages that
    keep the value out of ``innerText``: itch.io's API-keys page renders the
    key in a ``code.full_key`` element that stays hidden until the user
    clicks "View", so ``innerText`` never contains it.
    """

    trigger_url: str
    regex: str
    expression: str = INNER_TEXT_EXPRESSION


@dataclass
class AuthCaptureResult:
    """Outcome of an OAuth redirect capture attempt."""

    success: bool
    redirect_url: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def code(self) -> str | None:
        """Convenience: return the ``code`` query parameter if any."""
        return self.params.get("code")

    @property
    def state(self) -> str | None:
        """Convenience: return the ``state`` query parameter if any."""
        return self.params.get("state")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for RPC return values."""
        return {
            "success": self.success,
            "redirect_url": self.redirect_url,
            "params": dict(self.params),
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
        }
