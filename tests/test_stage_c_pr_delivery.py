"""A lost PR creation response is recovered by identity, never by another create."""
from copy import deepcopy
import subprocess

import pytest

from app import repo_guardian_github, stage_c_runner


SHA = "a" * 40
URL = "https://github.com/fixture/repo/pull/101"


def pull():
    return {"number": 101, "state": "open", "html_url": URL,
            "base": {"ref": "main", "repo": {"full_name": "Fixture/Repo"}},
            "head": {"ref": "task/TASK-0101", "sha": SHA,
                     "repo": {"full_name": "Fixture/Repo"}}}


def transport(monkeypatch, result, candidates):
    writes, reads = [], []

    def create(argv, **kwargs):
        writes.append(argv)
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(argv, 0, result, "")

    def lookup(path, expression):
        reads.append(path)
        if isinstance(candidates, Exception):
            raise candidates
        return candidates

    monkeypatch.setattr(stage_c_runner.subprocess, "run", create)
    monkeypatch.setattr(repo_guardian_github, "api", lookup)
    return writes, reads


@pytest.mark.parametrize("result", [
    subprocess.TimeoutExpired("gh", 60),
    subprocess.CalledProcessError(1, "gh", stderr="private response"),
    "", "unexpected response", "https://github.com/other/repo/pull/101",
])
def test_lost_or_invalid_create_response_recovers_one_exact_open_pr(monkeypatch, result):
    writes, reads = transport(monkeypatch, result, [pull()])
    opener = stage_c_runner.gh_pr_opener("Fixture/Repo", "main")
    assert opener("task/TASK-0101", "title", "body", SHA) == URL
    assert len(writes) == len(reads) == 1
    assert writes[0][:3] == ["gh", "pr", "create"]
    assert reads == ["repos/fixture/repo/pulls?state=open&head=fixture%3Atask%2FTASK-0101&base=main&per_page=2"]


def test_valid_create_response_does_not_poll_or_create_again(monkeypatch):
    writes, reads = transport(monkeypatch, URL.replace("fixture/repo", "Fixture/Repo"), [])
    assert stage_c_runner.gh_pr_opener("Fixture/Repo", "main")(
        "task/TASK-0101", "title", "body", SHA) == URL
    assert len(writes) == 1 and reads == []


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(state="closed"),
    lambda p: p["head"].update(sha="b" * 40),
    lambda p: p["head"].update(ref="task/TASK-0999"),
    lambda p: p["base"].update(ref="release"),
    lambda p: p["head"]["repo"].update(full_name="other/repo"),
    lambda p: p["base"]["repo"].update(full_name="other/repo"),
    lambda p: p["head"].update(repo=None),
    lambda p: p.pop("head"),
    lambda p: p.update(number=True),
    lambda p: p.update(number=0),
    lambda p: p.update(html_url="https://github.com/fixture/repo/pull/999"),
    lambda p: p.update(html_url="https://github.com/fixture/repo/pull/101?unknown=1"),
])
def test_unproven_pr_identity_fails_closed_without_repeating_create(monkeypatch, mutate):
    candidate = deepcopy(pull())
    mutate(candidate)
    writes, reads = transport(monkeypatch, subprocess.TimeoutExpired("gh", 60), [candidate])
    with pytest.raises(RuntimeError, match="publication could not be verified"):
        stage_c_runner.gh_pr_opener("fixture/repo", "main")("task/TASK-0101", "t", "b", SHA)
    assert len(writes) == len(reads) == 1


@pytest.mark.parametrize("candidates", [[], [pull(), pull()], {}, None, [None],
                                        RuntimeError("private API response")])
def test_missing_ambiguous_or_unavailable_lookup_does_not_authorize_delivery(monkeypatch, candidates):
    writes, reads = transport(monkeypatch, subprocess.TimeoutExpired("gh", 60), candidates)
    with pytest.raises(RuntimeError, match="publication could not be verified") as error:
        stage_c_runner.gh_pr_opener("fixture/repo", "main")("task/TASK-0101", "t", "b", SHA)
    assert "private" not in str(error.value)
    assert len(writes) == len(reads) == 1


@pytest.mark.parametrize("sha", [None, "", "main", "A" * 40, "a" * 39])
def test_creation_requires_the_exact_publish_commit(monkeypatch, sha):
    writes, reads = transport(monkeypatch, URL, [])
    with pytest.raises(ValueError):
        stage_c_runner.gh_pr_opener("fixture/repo", "main")("task/TASK-0101", "t", "b", sha)
    assert writes == reads == []


def fork_pull(head_repo="Agent/Repo"):
    candidate = pull()
    candidate["head"]["repo"]["full_name"] = head_repo
    return candidate


def test_b5_fork_pr_is_created_from_owner_branch_and_recovered_only_from_that_fork(monkeypatch):
    writes, reads = transport(monkeypatch, subprocess.TimeoutExpired("gh", 60), [fork_pull()])
    opener = stage_c_runner.gh_pr_opener("Fixture/Repo", "main", "agent/repo")
    assert opener("task/TASK-0101", "t", "b", SHA) == URL
    argv = writes[0]
    assert argv[argv.index("--repo") + 1] == "fixture/repo" and argv[argv.index("--head") + 1] == "agent:task/TASK-0101"
    assert reads == ["repos/fixture/repo/pulls?state=open&head=agent%3Atask%2FTASK-0101&base=main&per_page=2"]


@pytest.mark.parametrize("head_repo", ["Fixture/Repo", "attacker/repo", "agent/other"])
def test_b5_lookup_refuses_a_pr_whose_head_is_not_the_configured_fork(monkeypatch, head_repo):
    transport(monkeypatch, subprocess.TimeoutExpired("gh", 60), [fork_pull(head_repo)])
    with pytest.raises(RuntimeError, match="publication could not be verified"):
        stage_c_runner.gh_pr_opener("fixture/repo", "main", "agent/repo")("task/TASK-0101", "t", "b", SHA)


def test_b5_same_repository_publishing_keeps_the_bare_branch_head(monkeypatch):
    writes, _ = transport(monkeypatch, URL, [])
    stage_c_runner.gh_pr_opener("fixture/repo", "main")("task/TASK-0101", "t", "b", SHA)
    assert writes[0][writes[0].index("--head") + 1] == "task/TASK-0101"
