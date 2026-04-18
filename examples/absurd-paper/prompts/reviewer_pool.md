## Task

The paper is assembled and ready for peer review. Generate a manifest of **3 fake reviewers** with distinct personas, each suited to critique the paper from a different angle.

Return ONLY a JSON object with this exact shape:

```
{
  "items": [
    {
      "id": "maverick",
      "name": "<invented full name, e.g., Dr. Oluwaseun Adebayo-Park>",
      "affiliation": "<invented department + institution>",
      "style": "iconoclast",
      "focus": "questions foundational assumptions and methodological novelty"
    },
    {
      "id": "pedant",
      "name": "<invented full name>",
      "affiliation": "<invented department + institution>",
      "style": "methodologist",
      "focus": "scrutinizes statistical rigor and citation practices"
    },
    {
      "id": "applied",
      "name": "<invented full name>",
      "affiliation": "<invented department + institution>",
      "style": "practitioner",
      "focus": "evaluates real-world implications and replication prospects"
    }
  ]
}
```

Use the fixed `id` values: `maverick`, `pedant`, `applied`. Invent distinct names and institutions. Stay in character — these are serious-sounding academics.
