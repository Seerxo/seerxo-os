---
name: ci-debug
description: Diagnose and minimally repair one failing Company OS GitHub Actions run from its job and step evidence.
---

# CI debugging

Use the preparation checkout or supplied CI packet; the isolated Stage C coder has
neither Git metadata nor GitHub credentials. Reuse the existing GitHub CLI:

```sh
gh run view <run-id> --json headSha,status,conclusion,jobs
gh run view <run-id> --job <job-id> --log-failed
```

1. Confirm the failing run's commit and job. Read the failed step's relevant excerpt,
   expanding only when the first actionable error lacks context. Keep secrets out of
   captured artifacts and reports; never dump environment or authentication values.
2. Trace that error through the failing test or command to its implementation and
   callers. Separate the root cause from downstream failures and provider outages.
3. Make the smallest scoped fix. For a behavior bug, add or update a regression check
   that fails on the original behavior; configuration-only fixes need the applicable
   configuration check. Preserve unrelated files and policy boundaries.
4. Run focused checks, then account for the required gates using `verify-change`.
   Report run/commit identity, cause, changed files, commands and remaining gaps.

CI inspection grants no permission to change workflow policy, rerun remote jobs,
push, merge or deploy. Stop at the task's existing review and approval boundary.
