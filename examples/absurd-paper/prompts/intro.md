{{_retry_reason}}

## Context

The outline phase emitted this JSON (contains the abstract, buzzwords, and section beats):

```
{{outline}}
```

## Task

Write the **Introduction** section. Your output must be structured as follows:

```
## Abstract

<Copy the `abstract` field from {{outline}} verbatim as prose. Do NOT include JSON structure — only the abstract text itself, as a single paragraph.>

## Introduction

<The 400-600 word introduction described below.>
```

**Introduction requirements:**
- Use the "intro" beats from the outline as organizing guideposts.
- Cite 3-5 fake studies in `(Author et al., YYYY)` format. Pick plausible-sounding author surnames (Scandinavian, East Asian, continental European for gravitas). Years 1998-2024.
- Incorporate at least 3 buzzwords from the outline JSON.
- End with an explicit research-question sentence and an "In this paper, we…" roadmap sentence.
- Stay deadpan-academic.

Output ONLY that markdown — both the `## Abstract` block and the `## Introduction` block. No prose before or after. No code fences.
