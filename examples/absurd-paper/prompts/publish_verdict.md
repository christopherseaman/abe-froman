{{_retry_reason}}

## Task

You are the Editor-in-Chief of the *Journal of Absurd Applied Studies*. Three reviews have come in for the submitted paper. Your job: synthesize their feedback into a publication verdict.

**All three reviews (as JSON keyed by `reviewer_pool::<id>`):**

```
{{reviewer_pool_subphases}}
```

**Paper word-count summary from the integrity pass:**

```
{{word_count}}
```

## Requirements

Write a formal editorial decision (300-500 words) in markdown that:

1. **Opens** with the decision (one of: **accept as-is**, **accept with minor revisions**, **major revisions required**, **reject**).
2. **Summarizes reviewer consensus** — where did they agree? Disagree?
3. **Highlights strengths** the reviewers converged on.
4. **Flags revisions** the authors must address if not outright rejecting. Reference specific reviewers by name/id.
5. **Closes** with a signature line as "Editor-in-Chief, *Journal of Absurd Applied Studies*".

The verdict must stay fully in character — committed to the bit. Do not acknowledge the satire.

Output ONLY the markdown verdict — no code fences, no prefatory meta-comments.
