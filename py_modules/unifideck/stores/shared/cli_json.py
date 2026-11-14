"""Reading the JSON a store CLI prints on stdout.

Shared by the CLIs that print one JSON result line (``xodus-cli``,
``maxima-cli``). A warning or a progress line can come before the result, so
the last line that parses as an object wins.
"""

from __future__ import annotations

import json
from typing import Any


def last_json_object(stdout: str) -> dict[str, Any] | None:
    """The last stdout line that parses as a JSON object, or None.

    Commands print one JSON line, but a warning can come before it.
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None
