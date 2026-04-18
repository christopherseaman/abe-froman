{{_retry_reason}}

## Context

All four body sections are complete. Your job: assemble the final paper and bibliography as two files.

**Abstract:**

```
{{abstract}}
```

**Introduction:**

```
{{intro}}
```

**Methods:**

```
{{methods}}
```

**Results:**

```
{{results}}
```

**Discussion:**

```
{{discussion}}
```

## Task

Produce TWO files using the Write tool. **The paths are relative to the current working directory and must include the `../../` prefix so the files land in the workflow's shared staging directory, not inside the git worktree you are currently running in.**

### File 1: `../../paper/paper.md`

Structure:
```
# <Invented Title — should be catchy and sound like a real academic paper title>

**Author line:** Invent 2-4 authors with fake affiliations at implausible but real-sounding institutions (e.g., Department of Applied Oddities, ETH Zürich; Center for Speculative Ontology, Yale).

**Corresponding author:** <one of the invented authors>@<fake-edu-domain>

## Abstract
<the abstract JSON's "abstract" field, verbatim or lightly smoothed for prose>

## Introduction
<intro section verbatim, minus its existing `## Introduction` header since this one replaces it>

## Methods
<methods section, same treatment>

## Results
<results section, same treatment>

## Discussion
<discussion section, same treatment>

## Conclusion
<write a new 100-150 word conclusion synthesizing the paper — this is the only net-new prose>

## References
<see File 2; include a line here that reads "See `bibliography.md` for the full reference list.">
```

### File 2: `../../paper/bibliography.md`

Scan all four body sections for inline citations `(Author et al., YYYY)` and produce a consolidated references list in a `# References` markdown heading, formatted as a bulleted list:

```
# References

- Author, A., & Co-Author, B. (YYYY). Full invented paper title. *Invented Journal Name*, Vol(Issue), pages.
- ...
```

Deduplicate entries. Invent plausible journal names, volume/issue/page numbers for each one. Aim for 10-15 unique entries total across all sections. Stay in character — no real citations.

## Important

- Use the Write tool twice, with the exact paths `../../paper/paper.md` and `../../paper/bibliography.md`.
- The `../../` prefix is deliberate — it's how this workflow stages files outside per-phase worktrees. Do not substitute absolute paths or remove the prefix.
- After both Write calls succeed, respond with a short confirmation line (e.g., "Paper assembled: X words, Y references.") — do NOT re-include the paper content in your response.
