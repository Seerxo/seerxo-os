# Stage C source boundary — named commit blobs only

Source base: `337add0`, including #55 foundation and #57 offline sandbox proof.
This helper is not wired into the worker. Code-write and Guardian activation stay OFF.

## Problem / invariant

A worktree's `.git` file points to control-plane mirror metadata. Mounting that
metadata into a coder would expose remote configuration and mutating Git state.
`app/coder_source.py` instead exports an explicitly selected set of regular Git
blobs from an exact commit SHA into a fresh private directory. No whole-checkout
copy, Git metadata, symlink, submodule, untracked file or local edit crosses.

This preserves the existing Herdr/Stage C worktree seam: the control plane owns
the worktree and Git operations; the future coder sees only scoped source files.
No task engine, table, route, provider, image, service or permission is added.

## Contract

```python
manifest = export_source(repository, commit_sha, named_files, new_destination)
# {"commit": "<exact SHA>", "files": {"app/example.py": "<content sha256>"}}
```

The caller must resolve authorized ACTIVE project/task scope, audit source for
embedded secrets, and choose a private controller-owned parent directory. This
helper is not an authorization service or a semantic secret scanner. It never
selects files on a model's behalf. Credential/config/governance filename screening
is a conservative additional boundary, not proof that arbitrary code contains no
secret. Governing rules should be passed as read-only task context, not editable
files. A future diff-import gate must reject governance or out-of-scope changes.

The helper accepts at most 20 exact paths and 1 MiB total. It refuses moving refs,
path traversal, absolute paths, Git pathspec magic/globs, duplicate or portable
case/Unicode collisions, reserved metadata/credential/governance paths, directories,
symlinks and Git links. All validation and blob reads finish before output begins.
Reads have a 30-second total deadline and 10-second per-command limit. Git receives
only a minimal environment, so ambient `GIT_DIR`, injected global config and
replacement objects cannot redirect the export. Lazy fetching is disabled and an
empty remote-protocol allowlist also blocks repository-local remote transports;
missing promisor objects fail closed. No hooks, checkout filters,
package installation, network access or shell command is invoked.

Output allocation is exclusive and never overwrites an existing destination,
including a symlink. A copy failure removes only the directory allocated by that
invocation. The caller must finish export before exposing the directory to any
untrusted process; filesystem ownership must match the eventual approved sandbox
profile. Content hashes are returned to the control plane, not trusted from a coder.

## Verification / remaining gates

Tests use real disposable Git repositories: pinned bytes survive worktree drift;
ambient Git configuration is ignored; untracked files and metadata stay out;
unsafe scopes, symlinks, gitlinks, credential filenames, oversize data and existing
outputs fail; copy failure cleans up its own output while preserving source.
A synthetic promisor clone with a missing blob and remote-helper spy reproduces
the lazy-fetch defect before the guard, and proves no helper call or output after it.
The protocol guard also passes when the lazy-fetch variable is removed to simulate
an older Git ignoring that newer variable. This is a compatibility regression,
not certification of every historical Git build.
An additional local check exported `app/stage_c.py` from current main with content
SHA256 `9af36c1c16c8402f6e3d4900597f9c84da0d4aacc2df887b1fc5c3afe23cf27a`.
No production database or model credential was used.
The current production Stage-B worker has no Git executable. This helper is
therefore locally verified and unusable in that image; a reviewed control-plane
image/tooling change is required later. No package was installed on production.

This does not complete Stage C: still required are capability/command enforcement,
safe diff import and governance checks, reviewed provider egress/credential boundary,
independent fingerprint-bound review, control-plane-only push, bounded CI repair,
and approved worker wiring plus full golden tasks. No autonomous merge or deploy.

Rollback: remove the unused helper. No migration/configuration/activation changed.
New paid dependency/provider/infrastructure: none. Existing Actions allowance and
spending-limit verification remains a separate founder check.
