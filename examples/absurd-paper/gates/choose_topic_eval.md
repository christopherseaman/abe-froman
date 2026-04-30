## Task

You are the editorial quality gate for topic selection at the *Journal of Absurd Applied Studies*. Evaluate the chosen topic below for fitness to the journal's deadpan-absurd brief.

**Node output (JSON):**

```
{{output}}
```

## Scoring dimensions

Score each on a 0.0–1.0 scale:

- **absurdity** — Is the topic absurd in a mundane, deadpan way? Everyday life, domestic objects, social rituals, ambient phenomena. Computer security, AI, crypto, blockchain, programming, or generic tech-industry framings score low. An obviously-serious topic (e.g. "the thermodynamics of engines") also scores low — the absurdity comes from treating something trivial with method.
- **specificity** — Is the topic a pointable phenomenon rather than a broad field? "The X of Y in Z situation" scores well; "the study of small talk" scores poorly.
- **methodological_hook** — Does the phrasing suggest a real discipline (thermodynamics, rheology, ecology, semiotics, chronobiology, etc.) so the Methods section has somewhere to go?

## Output

Return ONLY this JSON — no prose, no code fences, no markdown:

```
{"absurdity": 0.0-1.0, "specificity": 0.0-1.0, "methodological_hook": 0.0-1.0, "feedback": "<1-2 sentences — if any dim is below its minimum, say concretely what to change>", "pass_criteria_met": ["..."], "pass_criteria_unmet": ["..."]}
```

Each dimension MUST be a **top-level numeric field** (not nested inside `"scores"`). The orchestrator derives the overall gate score from the per-dimension minimums declared in the workflow; do NOT emit a `"score"` field.

(Node id: `{{node_id}}`, attempt: `{{attempt}}`.)
