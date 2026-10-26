---
name: git
description: Inspect and change a repository with a reversible, reviewable workflow.
metadata:
  allowed_agents: "[developer, supervisor]"
  input: "repository path and task scope"
  output: "status, branch, diff and verification evidence"
  tools: "[git, hctl]"
  risks: "[overwriting_user_changes, destructive_reset, unreviewed_push]"
  verification: "Inspect status and diff, then run the task's checks."
---

# Git

Inspect status and existing changes first. Work on a task branch or herdr worktree.
Never reset, clean, push, merge or deploy outside the task's approval policy.

## Batch pushes and pull requests

Founder rule (2026-10-06). It started because each push cost a Vercel Desk deployment; the
Desk left Vercel the same day, and the rule stands until the founder changes it:

- Commit locally, one commit per change. Do not push or open a PR until the founder asks.
- Then push once and open one PR for the whole batch. Do not push again to that PR for
  small follow-ups; collect them and push once more only when needed (CI failure, review).
- Never one PR per fix. An urgent production fix may go alone; say why.
