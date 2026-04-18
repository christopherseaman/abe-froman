{{_retry_reason}}

## Context

All four body sections are complete. Compose the final paper as a single markdown document.

**Introduction section (already contains `## Abstract` + `## Introduction`):**

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

Output the complete paper as markdown text. Do NOT use any tools (no Write, no Bash, no Read). Just emit the paper as your text response.

Structure:

```
# <Invent a catchy academic title>

**Authors:** <Invent 2-4 fake authors with affiliations at implausible but real-sounding institutions>

**Corresponding author:** <fake>@<fake-edu-domain>

## Abstract
<Extract the abstract prose from the `## Abstract` block inside {{intro}}, verbatim.>

## Introduction
<Extract the introduction text from {{intro}} — everything after `## Introduction`. Do NOT duplicate the abstract.>

## Methods
<{{methods}} content, drop its leading `## Methods` header>

## Results
<{{results}} content, same treatment>

## Discussion
<{{discussion}} content, same treatment>

## Conclusion
<100-150 word new conclusion synthesizing the paper — the only net-new prose>

## References

<Scan all four sections for `(Author et al., YYYY)` citations. Produce a deduplicated bulleted list with invented journal names, volume/issue/page numbers. 10-15 unique entries.>
```

Stay in character throughout. Output the paper and NOTHING ELSE — no preamble, no meta-commentary, no tool use.
