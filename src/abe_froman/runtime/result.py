"""Unified result type for backends, executors, and subprocesses.

Conventions:
- success=True + output  → happy path
- success=False + error  → failure as value (executor owns retry policy)
- Backends raise OverloadError for 529/overload; executor catches and
  walks the model downgrade chain.
- Backends never set success=False directly — they raise on transport
  errors and let the executor classify them.

The executor layer owns retry policy; the backend layer owns transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ExecutionResult:
    success: bool = True
    output: str = ""
    error: str | None = None
    structured_output: dict[str, Any] | None = None
    tokens_used: dict[str, int] | None = None


class OverloadError(Exception):
    """Raised by a PromptBackend when the API returns 529/overloaded."""

    pass
