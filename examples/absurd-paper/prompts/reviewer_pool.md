## Task

The assembled paper is:

```
{{reconcile}}
```

Generate a manifest of **3 fake reviewers** with distinct personas. Each item must also carry a **shared 2-3 paragraph paper summary** so downstream per-reviewer children have enough context without re-reading the full paper.

Return ONLY a JSON object with this exact shape (no prose, no fences):

```
{
  "items": [
    {
      "id": "maverick",
      "name": "<invented full name, e.g., Dr. Oluwaseun Adebayo-Park>",
      "affiliation": "<invented department + institution>",
      "style": "iconoclast",
      "focus": "questions foundational assumptions and methodological novelty",
      "paper_summary": "<2-3 paragraph summary of the paper above — main thesis, methods sketch, key finding, implications. This exact text must be copied verbatim into every item's paper_summary below>"
    },
    {
      "id": "pedant",
      "name": "<invented full name>",
      "affiliation": "<invented department + institution>",
      "style": "methodologist",
      "focus": "scrutinizes statistical rigor and citation practices",
      "paper_summary": "<IDENTICAL summary as maverick above — copy verbatim>"
    },
    {
      "id": "applied",
      "name": "<invented full name>",
      "affiliation": "<invented department + institution>",
      "style": "practitioner",
      "focus": "evaluates real-world implications and replication prospects",
      "paper_summary": "<IDENTICAL summary as maverick above — copy verbatim>"
    }
  ]
}
```

Fixed `id` values: `maverick`, `pedant`, `applied`. Invent the rest.

The `paper_summary` field must be identical across all three items (same summary, three copies). Stay in character — these are serious-sounding academics reviewing the paper above.
