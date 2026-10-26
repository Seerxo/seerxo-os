#!/usr/bin/env python3
"""Refuse a production deploy unless the founder dispatched it for one exact commit already on main.

Merging a PR authorizes code; this authorizes a release. Inputs come from the workflow_dispatch event:
the actor who started the run AND the actor who triggered this attempt (a re-run by someone else must not
inherit the founder's authorization), the exact 40-hex commit, whether that commit is on main (checked
by git in the workflow; anything but "yes" is unknown and refused) and an optional, separate migration
authorization naming one exact 12-hex alembic revision.
"""
import argparse
import re
import sys

SHA = re.compile(r"[0-9a-f]{40}")
REVISION = re.compile(r"[0-9a-f]{12}")


def evaluate(*, actor: str, triggering_actor: str, founder: str, sha: str, allow_migration_to: str,
             sha_on_main) -> tuple[bool, str]:
    if not founder:
        return False, "no founder configured"
    if actor != founder or triggering_actor != founder:
        return False, f"dispatched by {actor!r} / triggered by {triggering_actor!r}, not the founder"
    if not SHA.fullmatch(sha or ""):
        return False, "sha must be exactly 40 lowercase hex characters"
    if sha_on_main is not True:
        return False, "the commit is not on main (or that could not be established)"
    if allow_migration_to and not REVISION.fullmatch(allow_migration_to):
        return False, "allow_migration_to must be empty or exactly 12 lowercase hex characters"
    return True, "deploy authorized for " + sha


def main() -> int:
    p = argparse.ArgumentParser()
    for name in ("--actor", "--triggering-actor", "--founder", "--sha", "--allow-migration-to", "--sha-on-main"):
        p.add_argument(name, required=True)
    a = p.parse_args()
    allowed, reason = evaluate(actor=a.actor, triggering_actor=a.triggering_actor, founder=a.founder, sha=a.sha,
                               allow_migration_to=a.allow_migration_to, sha_on_main=a.sha_on_main == "yes" or None)
    print(("DEPLOY AUTHORIZED: " if allowed else "DEPLOY REFUSED: ") + reason)
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
