## Task

You are the editorial quality gate for topic selection at the *Journal of Absurd Applied Studies*. Evaluate the chosen topic below for fitness to the journal's deadpan-absurd brief.

**Phase output (JSON):**

```
{{output}}
```

## Scoring dimensions

Score each on a 0.0–1.0 scale:

- **absurdity** — Is the topic absurd in a mundane, deadpan way? Everyday life, domestic objects, social rituals, ambient phenomena. Computer security, AI, crypto, blockchain, programming, or generic tech-industry framings score low. An obviously-serious topic (e.g. "the thermodynamics of engines") also scores low — the absurdity comes from treating something trivial with method.
- **specificity** — Is the topic a pointable phenomenon rather than a broad field? "The X of Y in Z situation" scores well; "the study of small talk" scores poorly.
- **methodological_hook** — Does the phrasing suggest a real discipline (thermodynamics, rheology, ecology, semiotics, chronobiology, etc.) so the Methods section has somewhere to go?

## Output

Return ONLY a JSON object with this exact shape (no prose, no code fence):

```
{
  "score": <weighted average, 0.0-1.0>,
  "scores": {
    "absurdity": <0.0-1.0>,
    "specificity": <0.0-1.0>,
    "methodological_hook": <0.0-1.0>
  },
  "feedback": "<1-2 sentences — if any dim < 0.7, say concretely what to change>",
  "pass_criteria_unmet": ["<short bullet per failing criterion, empty list if all pass>"]
}
```

Compute `score` as `(absurdity + specificity + methodological_hook) / 3`.
