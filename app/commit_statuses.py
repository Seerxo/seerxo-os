"""Commit statuses from one trusted publisher, as the gates read them.

CircleCI posts commit statuses (not Actions check runs). Each required context's LATEST status counts
(highest status id); a status from anyone else is a problem, never a result. Shared by the Stage C
merge gate, post-merge verification and Repository Guardian so they cannot disagree.
"""
STATUS_FAILED = {"failure", "error"}
STATUS_STATES = {"success", "pending"} | STATUS_FAILED
STATUS_PAGE = 100
STATUS_QUERY = "[.[] | {id, context, state, created_at, creator: {id: .creator.id, login: .creator.login}}]"


def latest_statuses(raw, contexts: tuple[str, ...], publisher: dict) -> dict:
    """Latest commit status per required context, from the trusted publisher only.

    Returns {"statuses": {context: state}, "status_problems": [...], "statuses_complete": bool}.
    Fail closed: a required context posted by anyone else (a spoof), any malformed entry, an
    unknown state, or a full page (more statuses may exist beyond it) is a problem, never ignored.
    Newest wins by status id, so an older success never outranks a newer pending or failure.
    """
    if not isinstance(raw, list):
        return {"statuses": {}, "status_problems": ["status list is malformed"], "statuses_complete": False}
    problems, latest = [], {}
    for item in raw:
        creator = item.get("creator") if isinstance(item, dict) else None
        well_formed = (isinstance(item, dict) and isinstance(item.get("id"), int)
                       and not isinstance(item.get("id"), bool) and isinstance(item.get("context"), str)
                       and isinstance(item.get("state"), str) and isinstance(creator, dict))
        if not well_formed:
            problems.append("malformed status entry")
            continue
        context = item["context"]
        if context not in contexts:
            continue
        if creator.get("id") != publisher["id"] or creator.get("login") != publisher["login"]:
            problems.append(f"{context} was posted by an untrusted publisher")
            continue
        if item["state"] not in STATUS_STATES:
            problems.append(f"{context} has unknown state {item['state']!r}")
            continue
        if context not in latest or item["id"] > latest[context]["id"]:
            latest[context] = item
    return {"statuses": {context: item["state"] for context, item in latest.items()},
            "status_ids": {context: item["id"] for context, item in latest.items()},
            "status_problems": problems, "statuses_complete": len(raw) < STATUS_PAGE}


def status_checks(statuses: dict, contexts: tuple[str, ...]) -> list[dict]:
    """Latest CircleCI statuses as check-shaped facts, so every gate reads one shape. A missing
    context has no entry: the caller's completeness check refuses it."""
    checks = []
    for context in contexts:
        state = statuses.get(context)
        if state is None:
            continue
        if state == "pending":
            checks.append({"name": context, "status": "in_progress", "conclusion": None})
        else:
            checks.append({"name": context, "status": "completed",
                           "conclusion": "success" if state == "success" else "failure"})
    return checks
