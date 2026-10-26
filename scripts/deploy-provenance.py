#!/usr/bin/env python3
"""Refuse a production release unless GitHub proves the founder merged this commit."""
import argparse
import json
import re
import sys

SHA = re.compile(r"[0-9a-f]{40}")


def evaluate(prs: list[dict], sha: str, founder: str) -> tuple[bool, str]:
    if not SHA.fullmatch(sha):
        return False, "pushed SHA is invalid"
    matches = []
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "closed" or not pr.get("merged"):
            continue
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        merger = pr.get("merged_by") if isinstance(pr.get("merged_by"), dict) else {}
        if (base.get("ref") == "main" and merger.get("login") == founder
                and pr.get("merge_commit_sha") == sha):
            matches.append(pr)
    if len(matches) == 1:
        return True, "PR merge provenance verified"
    if len(matches) > 1:
        return False, "ambiguous: multiple founder-merged PRs match this commit"
    merged = [pr for pr in prs if isinstance(pr, dict) and pr.get("state") == "closed" and pr.get("merged")]
    if not merged:
        return False, "no merged PR is associated with this commit"
    if not any(isinstance(pr.get("base"), dict) and pr["base"].get("ref") == "main" for pr in merged):
        return False, "associated PR base is not main"
    if not any(isinstance(pr.get("merged_by"), dict) and pr["merged_by"].get("login") == founder
                   for pr in merged):
        return False, "associated PR was not merged by the founder"
    return False, "associated PR merge commit is not this pushed commit"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--founder", default="semihbugrasezer")
    args = parser.parse_args()
    try:
        prs = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("DEPLOY REFUSED: invalid GitHub PR evidence")
        return 1
    if not isinstance(prs, list):
        print("DEPLOY REFUSED: invalid GitHub PR evidence")
        return 1
    allowed, reason = evaluate(prs, args.sha, args.founder)
    print(("DEPLOY ALLOWED: " if allowed else "DEPLOY REFUSED: ") + reason)
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
