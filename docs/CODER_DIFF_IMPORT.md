# Stage C diff boundary — untrusted workspace back to the control plane

The reverse of `docs/CODER_SOURCE_EXPORT.md`. `app/coder_diff.py` is not wired into the
worker. Code-write and Guardian activation stay OFF.

## Flow

```text
export_source(repo, SHA, files, base)    control plane keeps this pristine copy
export_source(repo, SHA, files, work)    mounted into the coder sandbox
        … coder edits work/ …
import_changes(base, work, manifest, allowed_new)  -> diff, fingerprint F
        … tests + independent review approve F …
apply_to_worktree(task/<KEY> worktree, diff, F)     control plane only; then push + PR
```

## What crosses back

- **Accepted:** files that were exported and modified, plus new files named in the task's
  `allowed_new`.
- **Paths:** every path must pass `coder_source._safe_path`. That excludes `.git`, `.env*`,
  keys, `CLAUDE.md`/`AGENTS.md`, `agents/`, `.github/`, Docker/Compose and policy files.
- **Content:** regular single-link files only, UTF-8 text without NUL bytes, at most
  20 files, 256 KiB per file and 1 MiB in total.
- **Refused:** deletions, symlinks (including symlinked directories), hard links, FIFOs
  and devices, and new files outside scope. Mode-only changes produce no content diff and
  are ignored. The worktree keeps the committed mode.
- **Base integrity:** the base must hash exactly to the export manifest; a tampered base is
  refused, because it would hide changes.

The diff is rendered by `git diff --no-index` over copies in a private scratch directory,
never inside the workspace. Git runs with no system or global config, no external diff,
no textconv and no network protocols.

`apply_to_worktree` recomputes the fingerprint and refuses on any byte difference. It
re-checks every path in the patch (`git apply --numstat`) and runs `git apply --check`
before applying.

## Tests

`tests/test_coder_diff.py` covers:
- a round trip (modified file, new file, a file without a final newline) applied
  byte-for-byte to a real `task/<KEY>` worktree;
- a changed diff refused against the approved fingerprint;
- ten workspace attacks: symlink, hard link, unscoped new file, `.env`, `.git/`,
  deletion, binary, oversize, FIFO, governance file;
- a tampered base and a no-op import.

Mutations that drop the hard-link check or the path check fail the suite.

## Not yet

Wiring into the worker, the coder container run itself, the independent review call, and
the control-plane push/PR. Each one needs separate founder approval (see
AUTONOMOUS_AGENT_ARCHITECTURE.md §12).
