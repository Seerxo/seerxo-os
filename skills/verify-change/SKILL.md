---
name: verify-change
description: Select and run focused checks for a Company OS change, then account for required repository gates.
---

# Change verification

1. Inspect the changed files and behavior, including new files. Use `git diff` in a
   checkout or the supplied diff/manifest in a Stage C packet.
2. Run the smallest check that exercises the changed behavior. Reuse existing tests
   and commands; choose affected callers as well as the directly edited module.
3. Resolve failures before broadening. Read `.github/workflows/ci.yml` for required
   gates; reuse its commands and isolation requirements rather than maintaining a
   second test matrix here. Focused tests do not replace those gates.
4. Report exact commands, exit status, passed checks and checks not run with reasons.
   Missing dependencies, skipped integration tests and unavailable services are gaps,
   not success. Distinguish local verification from CI and live behavior.

For Stage C, the control plane runs checks on its disposable copy using the existing
`checks` callback and launcher profile. The coder does not gain host or production
access to run verification. Passing checks leaves the change ready for independent
review; founder acceptance is still required for task completion.
