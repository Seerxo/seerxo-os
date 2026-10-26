"""Only a founder's merged PR may release a tested main commit."""
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "deploy_provenance", Path(__file__).parents[1] / "scripts" / "deploy-provenance.py")
provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provenance)

SHA = "a" * 40
FOUNDER = "semihbugrasezer"


def merged_pr(**overrides):
    pr = {
        "state": "closed",
        "merged": True,
        "base": {"ref": "main"},
        "merged_by": {"login": FOUNDER},
        "merge_commit_sha": SHA,
    }
    pr.update(overrides)
    return pr


def test_accepts_the_exact_main_merge_by_the_founder():
    assert provenance.evaluate([merged_pr()], SHA, FOUNDER) == (True, "PR merge provenance verified")


@pytest.mark.parametrize("prs,reason", [
    ([], "no merged PR is associated with this commit"),
    ([merged_pr(merged_by={"login": "someone-else"})], "not merged by the founder"),
    ([merged_pr(base={"ref": "release"})], "not main"),
    ([merged_pr(merge_commit_sha="b" * 40)], "not this pushed commit"),
    ([merged_pr(merged=False)], "no merged PR is associated with this commit"),
    ([merged_pr(state="open")], "no merged PR is associated with this commit"),
    ([merged_pr(base=[]), {"merged": True}], "not main"),
    ([merged_pr(), merged_pr()], "ambiguous"),
])
def test_refuses_direct_or_untrusted_main_pushes(prs, reason):
    allowed, actual = provenance.evaluate(prs, SHA, FOUNDER)
    assert allowed is False
    assert reason in actual


def test_rejects_an_invalid_pushed_sha():
    assert provenance.evaluate([merged_pr()], "not-a-sha", FOUNDER) == (False, "pushed SHA is invalid")
