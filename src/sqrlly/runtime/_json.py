"""Tolerant JSON extraction from LLM output.

One extractor serves both JSON-consuming seams — gate verdicts
(`runtime/gates.py`) and fan-out manifests (`compile/_manifest.py`) —
so their tolerance for common LLM wrappings can't drift apart.
"""
from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)


def extract_json(text: str, *, objects_only: bool = False) -> str:
    """Pull a JSON payload out of common LLM wrappings: a ``` code
    fence, or a payload embedded in a reasoning preamble. Returns the
    candidate substring, or the original text when neither applies (the
    caller's ``json.loads`` decides — genuine garbage still fails there).

    ``objects_only=True`` scans for ``{...}`` only — REQUIRED for gate
    verdicts, where a ``[`` in the reasoning preamble (e.g. "range is
    [0,1]") would otherwise widen the slice and break parsing. The
    default array-aware scan serves fan-out manifests, which may
    legitimately be bare JSON arrays.
    """
    t = text.strip()
    fence = _FENCE_RE.search(t)
    if fence:
        return fence.group(1).strip()
    if objects_only:
        start, end = t.find("{"), t.rfind("}")
        if 0 <= start < end:
            return t[start : end + 1]
        return t
    starts = [i for i in (t.find("["), t.find("{")) if i != -1]
    ends = [i for i in (t.rfind("]"), t.rfind("}")) if i != -1]
    if starts and ends:
        start, end = min(starts), max(ends)
        if start <= end:
            return t[start : end + 1]
    return t
