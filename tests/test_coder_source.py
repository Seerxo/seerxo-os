"""Source export does not expose mirror metadata, ambient files or unsafe paths."""
import hashlib
import shlex
import subprocess

import pytest

from app import coder_source


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q', '-b', 'main')
    (root / 'app').mkdir()
    (root / 'app' / 'safe.py').write_text('value = 1\n')
    (root / '.env').write_text('synthetic fixture only')
    (root / 'CLAUDE.md').write_text('synthetic governance fixture')
    (root / 'link.py').symlink_to('app/safe.py')
    git('add', '.')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    return root, git('rev-parse', 'HEAD'), git


def test_exports_pinned_blobs_only_without_worktree_or_git_metadata(repo, tmp_path, monkeypatch):
    root, sha, _ = repo
    (root / 'app' / 'safe.py').write_text('uncommitted drift')
    (root / 'untracked.txt').write_text('ambient fixture')
    monkeypatch.setenv('GIT_DIR', '/nonexistent/ambient-git-dir')
    destination = tmp_path / 'export'
    manifest = coder_source.export_source(root, sha, ['app/safe.py'], destination)
    assert (destination / 'app/safe.py').read_text() == 'value = 1\n'
    assert not (destination / '.git').exists()
    assert not (destination / '.env').exists()
    assert not (destination / 'untracked.txt').exists()
    assert manifest == {'commit': sha, 'files': {'app/safe.py':
        hashlib.sha256(b'value = 1\n').hexdigest()}}


def test_rejects_unsafe_scope_before_creating_any_output(repo, tmp_path):
    root, sha, _ = repo
    for path in ('../outside', '/etc/passwd', '.git/config', '.env', 'CLAUDE.md',
                 'app', 'link.py', 'missing.py', ':(glob)*', 'app/*.py',
                 'auth.json', 'key.pem', 'docs/CONSTITUTION_REFERENCE.md',
                 '.github/workflows/ci.yml', 'agents/registry.yaml', 'docker-compose.yml'):
        destination = tmp_path / 'export'
        with pytest.raises(ValueError):
            coder_source.export_source(root, sha, ['app/safe.py', path], destination)
        assert not destination.exists()
    for revision in ('main', 'HEAD', '--help', '', sha + ':app/safe.py'):
        with pytest.raises(ValueError):
            coder_source.export_source(root, revision, ['app/safe.py'], tmp_path / 'export')
    for scope in ([], ['app/safe.py'] * 2, [f'file-{i}.py' for i in range(21)]):
        with pytest.raises(ValueError):
            coder_source.export_source(root, sha, scope, tmp_path / 'export')


def test_export_never_overwrites_and_rejects_oversized_blobs(repo, tmp_path):
    root, sha, git = repo
    destination = tmp_path / 'existing'
    destination.mkdir()
    (destination / 'keep').write_text('preserve')
    with pytest.raises(FileExistsError):
        coder_source.export_source(root, sha, ['app/safe.py'], destination)
    assert (destination / 'keep').read_text() == 'preserve'
    (root / 'large.txt').write_bytes(b'x' * (1024 * 1024 + 1))
    git('add', 'large.txt')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'large')
    with pytest.raises(ValueError):
        coder_source.export_source(root, git('rev-parse', 'HEAD'), ['large.txt'], tmp_path / 'export')
    assert not (tmp_path / 'export').exists()


def test_gitlinks_and_credential_key_names_cannot_cross(repo, tmp_path):
    root, sha, git = repo
    git('update-index', '--add', '--cacheinfo', f'160000,{sha},submodule')
    (root / 'id_ed25519').write_text('synthetic fixture only')
    git('add', 'id_ed25519')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'gitlink/key')
    sha = git('rev-parse', 'HEAD')
    for path in ('submodule', 'id_ed25519'):
        with pytest.raises(ValueError):
            coder_source.export_source(root, sha, [path], tmp_path / 'export')
        assert not (tmp_path / 'export').exists()


def test_cleanup_after_copy_failure_never_touches_source(repo, tmp_path, monkeypatch):
    from pathlib import Path
    root, sha, _ = repo
    destination = tmp_path / 'export'
    original_write = Path.write_bytes
    def fail_write(path, data):
        if destination in path.parents:
            raise OSError('synthetic copy failure')
        return original_write(path, data)
    monkeypatch.setattr(Path, 'write_bytes', fail_write)
    with pytest.raises(OSError):
        coder_source.export_source(root, sha, ['app/safe.py'], destination)
    assert not destination.exists()
    assert (root / 'app/safe.py').read_text() == 'value = 1\n'


def test_portable_path_collisions_are_rejected_before_writes(repo, tmp_path):
    root, sha, git = repo
    oid = git('rev-parse', f'{sha}:app/safe.py')
    for name in ('app/A.py', 'app/a.py'):
        git('-c', 'core.ignorecase=false', 'update-index', '--add', '--cacheinfo', f'100644,{oid},{name}')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'case collision')
    with pytest.raises(ValueError):
        coder_source.export_source(root, git('rev-parse', 'HEAD'),
                                   ['app/A.py', 'app/a.py'], tmp_path / 'export')
    assert not (tmp_path / 'export').exists()


@pytest.mark.parametrize('ignore_lazy_fetch_guard', [False, True])
def test_missing_promisor_blob_fails_without_invoking_remote_helper(repo, tmp_path, monkeypatch,
                                                                  ignore_lazy_fetch_guard):
    root, sha, git = repo
    oid = git('rev-parse', f'{sha}:app/safe.py')
    marker = tmp_path / 'transport-called'
    spy = tmp_path / 'fetch-spy.sh'
    spy.write_text(f'printf called > {shlex.quote(str(marker))}\nexit 1\n')
    git('remote', 'add', 'origin', 'ext::sh ' + str(spy).replace('%', '%%').replace(' ', '% '))
    git('config', 'protocol.ext.allow', 'always')
    git('config', 'core.repositoryformatversion', '1')
    git('config', 'extensions.partialClone', 'origin')
    git('config', 'remote.origin.promisor', 'true')
    # Only the synthetic fixture's loose blob is removed, never an actual checkout.
    (root / '.git' / 'objects' / oid[:2] / oid[2:]).unlink()
    if ignore_lazy_fetch_guard:
        # Older Git may ignore this newer guard; deny-all protocols must still hold.
        run = subprocess.run
        def without_lazy_fetch_guard(*args, **kwargs):
            if 'env' in kwargs:
                env = dict(kwargs['env'])
                env.pop('GIT_NO_LAZY_FETCH', None)
                kwargs['env'] = env
            return run(*args, **kwargs)
        monkeypatch.setattr(subprocess, 'run', without_lazy_fetch_guard)
    with pytest.raises(subprocess.CalledProcessError):
        coder_source.export_source(root, sha, ['app/safe.py'], tmp_path / 'export')
    assert not marker.exists(), 'export invoked a remote transport helper'
    assert not (tmp_path / 'export').exists()
