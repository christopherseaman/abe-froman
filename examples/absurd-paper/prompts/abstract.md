{{_retry_reason}}

## Task

The seed command has emitted the following research domain:

```
{{seed_topic}}
```

Write an abstract for a research paper on this domain. The abstract must be 150-220 words and include:

1. **Background** (2-3 sentences) framing the domain as an understudied but important field.
2. **Research question** (1 sentence) posed with academic precision.
3. **Methodological hint** (1-2 sentences) naming the approach — invent a methodological acronym if helpful.
4. **Key finding** (2-3 sentences) with a specific numeric result (p-value, effect size, confidence interval, or invented unit).
5. **Implications** (1-2 sentences) gesturing at practice or policy.

Also produce a short list of **buzzwords** the subsequent sections will re-use so the paper feels cohesive. Invent 4-6 terms or acronyms.

Return ONLY a JSON object with this exact shape (no prose, no code fence):

```
{"abstract": "<the abstract, 150-220 words>", "buzzwords": ["term1", "term2", ...]}
```

Stay in character — refer to the preamble style guide.
