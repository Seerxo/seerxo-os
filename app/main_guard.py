"""B5 interim detective control: every commit on Seerxo/seerxo-os `main` must be a pull request merged by the founder.

It DETECTS, it does not prevent: GitHub Free offers no branch protection for a private repository, so a Write token
can still push or merge. Production is protected by the release gateway, never by this alarm. A GitHub login is not
a person either; this only tells the founder that `main` moved in a way he did not make through a reviewed PR.

Walks `main`'s first-parent chain from the last checked head. Each commit must be the merge (or squash) commit of a
merged PR whose `merged_by` is the founder; anything else is MAIN_UNEXPECTED_CHANGE (critical, Telegram). When the
check cannot run (no token, GitHub error, history rewritten or too long) it reports MAIN_GUARD_DEGRADED instead of
staying silent. Read-only on GitHub. Runs in company-worker from host cron:
    docker exec company-worker python -m app.main_guard
"""
import os
import sys

from app import enums, models
from app.notify import fingerprint, notifier

REPOSITORY = "Seerxo/seerxo-os"
BRANCH = "main"
FOUNDERS = frozenset({"semihbugrasezer"})
CHECKPOINT = "main-guard"
MAX_WALK = 50


class Degraded(Exception):
    pass


def check(fetch, last_head: str | None) -> tuple[str, list[str]]:
    """(current head, problems). fetch(path) -> parsed JSON, raising on any HTTP or network error."""
    commits = fetch(f"/repos/{REPOSITORY}/commits?sha={BRANCH}&per_page={MAX_WALK}")
    if not isinstance(commits, list) or not commits:
        raise Degraded("no commits returned for main")
    by_sha = {c["sha"]: c for c in commits}
    head = commits[0]["sha"]
    if last_head is None or head == last_head:
        return head, []                       # first run records a baseline; no movement, nothing to judge
    chain, sha = [], head
    while sha != last_head:
        commit = by_sha.get(sha)
        if commit is None:
            raise Degraded(f"previous head {last_head[:12]} not within the last {MAX_WALK} commits "
                           "(history rewritten or too many commits): main must be checked by hand")
        chain.append(sha)
        sha = (commit.get("parents") or [{}])[0].get("sha")
    problems = []
    for sha in reversed(chain):
        pulls = fetch(f"/repos/{REPOSITORY}/commits/{sha}/pulls")
        merged = [p for p in pulls if p.get("merge_commit_sha") == sha and p.get("merged_at")]
        if not merged:
            problems.append(f"{sha[:12]}: on main without a merged pull request (direct push)")
            continue
        pr = fetch(f"/repos/{REPOSITORY}/pulls/{merged[0]['number']}")
        who = ((pr.get("merged_by") or {}).get("login")) or "unknown"
        if who not in FOUNDERS:
            problems.append(f"{sha[:12]}: PR #{pr.get('number')} merged by {who}, not the founder")
    return head, problems


def _record(db, event_type: str, severity: enums.Severity, title: str, detail: str) -> None:
    ev = models.Event(event_type=event_type, source="main-guard", severity=severity,
                      payload={"title": title, "message": detail[:400]},
                      dedupe_key=fingerprint(event_type, "main-guard", title + detail[:200]))
    db.add(ev)
    db.commit()
    db.refresh(ev)
    notifier.dispatch(db, ev)


def run(db, fetch) -> dict:
    row = db.get(models.ControlState, CHECKPOINT)
    last = (row.value or {}).get("head") if row else None
    try:
        head, problems = check(fetch, last)
    except Exception as error:            # any failure is reported, never a silent pass
        _record(db, "MAIN_GUARD_DEGRADED", enums.Severity.MEDIUM, "main guard could not check Seerxo/seerxo-os main",
                f"{type(error).__name__}: {error}")
        return {"status": "DEGRADED", "error": type(error).__name__}
    if problems:
        _record(db, "MAIN_UNEXPECTED_CHANGE", enums.Severity.HIGH,
                f"Seerxo/seerxo-os main changed outside a founder-merged PR ({len(problems)})", "; ".join(problems))
    if row is None:
        db.add(models.ControlState(key=CHECKPOINT, value={"head": head}, updated_by="main-guard"))
    else:
        row.value, row.updated_by = {"head": head}, "main-guard"
    db.commit()
    return {"status": "ALERT" if problems else "OK", "head": head, "problems": problems}


def _github_fetch(token: str):
    import httpx
    client = httpx.Client(base_url="https://api.github.com", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})

    def fetch(path: str):
        r = client.get(path)
        r.raise_for_status()
        return r.json()
    return fetch


def main() -> int:
    from app.db import SessionLocal
    token = os.environ.get("GH_TOKEN", "")
    with SessionLocal() as db:
        if not token:
            _record(db, "MAIN_GUARD_DEGRADED", enums.Severity.MEDIUM, "main guard could not check Seerxo/seerxo-os main",
                    "GH_TOKEN is not set in company-worker")
            print("main-guard: DEGRADED (no GH_TOKEN)")
            return 2
        out = run(db, _github_fetch(token))
    print(f"main-guard: {out['status']}")
    return 0 if out["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
