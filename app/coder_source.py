"""Export named committed blobs, never a checkout or its Git metadata.

Unused Stage C prerequisite. Caller owns project/task authorization and must audit
source for embedded secrets; filename screening is not a semantic secret scanner.
"""
import hashlib
import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path, PurePosixPath


def _safe_path(path):
    if not isinstance(path, str) or not path or any(c in path for c in '\\:*?[]'):
        return False
    p = PurePosixPath(path)
    parts = path.split('/')
    if (p.is_absolute() or str(p) != path or any(part in ('', '.', '..') for part in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        return False
    lowered = [part.lower() for part in parts]
    restricted = {'.git', '.ssh', '.aws', '.codex', '.claude', '.github', 'agents'}
    names = {'claude.md', 'agents.md', 'constitution_reference.md', 'auth.json',
             'credentials.json', '.netrc', '.npmrc', '.pypirc', '.gitmodules',
             'dockerfile', 'compose.yml', 'compose.yaml', 'id_rsa', 'id_ed25519',
             'id_ecdsa', 'id_dsa', 'authorized_keys'}
    return not (restricted.intersection(lowered) or any(part in names for part in lowered)
                or any(part.startswith('.env') or part.endswith(('.env', '.pem', '.key', '.p12', '.pfx'))
                       or part.startswith(('docker-compose', 'cost_policy.', 'approval_policy.', 'security_policy.'))
                       for part in lowered))


def export_source(repository: Path, commit: str, paths: list[str], destination: Path) -> dict:
    """Fresh directory, at most 20 exact files / 1 MiB total, pinned SHA + content hashes.

    Validation and bounded object reads finish before any destination is created.
    No Git metadata, worktree drift, untracked files, symlinks or submodules cross.
    """
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Use an exact commit SHA, not a moving revision')
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 20
            or not all(_safe_path(path) for path in paths)
            or len({unicodedata.normalize('NFC', path).casefold() for path in paths}) != len(paths)):
        raise ValueError('Scope must contain bounded exact safe file paths')
    repository, destination = Path(repository), Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError('Source export never overwrites an existing destination')
    deadline = time.monotonic() + 30
    # No inherited GIT_DIR, config injection, replacement objects or host auth env.
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C', 'LC_ALL': 'C',
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': ''}
    def git(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Source export read deadline exceeded')
        return subprocess.run(['git', '--no-pager', '-C', str(repository), *args],
                              env=env, check=True, capture_output=True,
                              timeout=min(10, remaining)).stdout
    if git('cat-file', '-t', commit).strip() != b'commit':
        raise ValueError('Source identity must name a commit object')
    entries = {}
    for entry in git('ls-tree', '-z', '--full-tree', commit, '--', *paths).split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        name = name.decode('utf-8')
        if name not in paths or mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError('Only selected regular committed files may be exported')
        entries[name] = (mode, oid.decode('ascii'))
    if set(entries) != set(paths):
        raise ValueError('A selected file is missing or is not a regular file')
    total, contents = 0, {}
    for path, (mode, oid) in entries.items():
        size = int(git('cat-file', '-s', oid))
        total += size
        if total > 1024 * 1024:
            raise ValueError('Source export exceeds the 1 MiB bound')
        data = git('cat-file', 'blob', oid)
        if len(data) != size:
            raise ValueError('Source object size changed during export')
        contents[path] = (mode, data)
    destination.mkdir(mode=0o700)  # exclusive allocation; racing exporters cannot overwrite
    try:
        for path, (mode, data) in contents.items():
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if mode == b'100755' else 0o644)
    except BaseException:
        shutil.rmtree(destination)  # only the directory allocated by this invocation
        raise
    return {'commit': commit, 'files': {path: hashlib.sha256(data).hexdigest()
                                       for path, (_, data) in contents.items()}}
