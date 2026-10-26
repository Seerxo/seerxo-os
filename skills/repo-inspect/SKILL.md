---
name: repo-inspect
description: Find the scoped files, current behavior and relevant checks before a Company OS repository change.
---

# Repository inspection

Work in the preparation checkout. The Stage C coder receives selected files without
Git metadata; use its supplied task scope and manifest instead of repository commands.

1. Read the applicable repository instructions, then `git status --short --branch`.
   Preserve existing changes and identify who owns files that overlap the task.
2. Use `rg` to find the task's entry point, callers and relevant tests. Read those
   files; follow dependencies only until the behavior and change boundary are clear.
3. Inspect the relevant diff. Consult recent commits or CI evidence only when they
   explain the task. Read `.github/workflows/ci.yml` for required checks.
4. Return the relevant paths, observed behavior, smallest proposed change and exact
   verification commands. Mark unavailable evidence explicitly.

Load `company-state` only when operational database state affects the task. Inspection
does not authorize production access or broaden the coder's allowed file scope.
