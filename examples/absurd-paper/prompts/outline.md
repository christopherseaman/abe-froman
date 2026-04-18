{{_retry_reason}}

## Context

The abstract for this paper:

```
{{abstract}}
```

## Task

Produce a structured outline for the paper with exactly four sections: **intro, methods, results, discussion**. For each section, produce 3-5 "beats" — concrete sub-points the section's author should develop.

Return ONLY a JSON object with this exact shape (no prose, no code fence). The `abstract` and `buzzwords` fields MUST be copied verbatim from the abstract phase's output so downstream section authors have a single source of truth:

```
{
  "abstract": "<the prose abstract text, copied verbatim from the `abstract` field of the abstract phase JSON>",
  "buzzwords": ["<copied verbatim from the `buzzwords` array of the abstract phase JSON>", "..."],
  "items": [
    {"id": "intro",      "title": "Introduction",       "beats": ["beat 1", "beat 2", "beat 3"]},
    {"id": "methods",    "title": "Methods",            "beats": ["...", "...", "..."]},
    {"id": "results",    "title": "Results",            "beats": ["...", "...", "..."]},
    {"id": "discussion", "title": "Discussion",         "beats": ["...", "...", "..."]}
  ]
}
```

All four sections must be present in this order. Use buzzwords in at least one beat per section. Stay in character.
