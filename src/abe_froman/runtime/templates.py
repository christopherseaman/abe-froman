"""Template rendering, model resolution, and model downgrade.

These helpers are reusable across prompt execution strategies — they
don't depend on the backend transport layer. A future feature could
reuse render_template for templated output contract paths or CLI
--var substitution without dragging in the prompt executor.
"""

from __future__ import annotations

import re
from typing import Any

from abe_froman.schema.models import Phase, Settings

MODEL_DOWNGRADE_CHAIN = ["opus", "sonnet", "haiku"]


def downgrade_model(current: str) -> str | None:
    """Return the next model in the downgrade chain, or None if at the bottom."""
    try:
        idx = MODEL_DOWNGRADE_CHAIN.index(current)
    except ValueError:
        return None
    if idx + 1 < len(MODEL_DOWNGRADE_CHAIN):
        return MODEL_DOWNGRADE_CHAIN[idx + 1]
    return None


def render_template(template: str, context: dict[str, Any]) -> str:
    """Replace {{variable}} placeholders with values from context.

    Leaves unresolved placeholders intact.
    """

    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        if key in context:
            return str(context[key])
        return match.group(0)

    return re.sub(r"\{\{(\s*\w+\s*)\}\}", replacer, template)


def resolve_model(phase: Phase, settings: Settings) -> str:
    """Phase model > settings default_model."""
    return phase.model or settings.default_model
