from copy import deepcopy

import pytest
import yaml

from app import project_profiles, stage_c_worker
from tests.test_worker import _db, _task

PILOT = "seerxo/os"
YOUTUBE = "semihbugrasezer/youtube-auto"


def _raw():
    return yaml.safe_load(project_profiles.PROFILE_PATH.read_text())


def _write(tmp_path, raw):
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def test_registry_keeps_company_writer_and_registers_youtube_read_only():
    policy = project_profiles.load()
    company = project_profiles.for_project(policy, "company-os", PILOT)
    youtube = project_profiles.for_project(policy, "youtube-auto", YOUTUBE)
    assert project_profiles.writable_repositories(policy) == [PILOT]
    assert company["write_authorized"] is True
    assert company["target_branch"] == "main"
    assert set(company["allowed_paths"]) == {
        "app/goals.py", "app/metrics_store.py", "app/packaging.py",
        "tests/test_goals.py", "tests/test_metrics.py", "tests/test_packaging.py",
    }
    assert youtube["business_unit"] == "youtube"
    assert youtube["target_branch"] == "server-master"
    assert youtube["write_authorized"] is False
    assert youtube["allowed_paths"] == [] and youtube["execution_profile"] is None


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(extra=True),
    lambda raw: raw["projects"]["company-os"].update(extra=True),
    lambda raw: raw["projects"]["company-os"]["repositories"][PILOT].update(extra=True),
    lambda raw: raw["projects"]["company-os"]["repositories"][PILOT].update(allowed_paths=["../.env"]),
    lambda raw: raw["projects"]["youtube-auto"]["repositories"][YOUTUBE].update(allowed_paths=["src/x.py"]),
    lambda raw: raw["projects"]["youtube-auto"]["repositories"][YOUTUBE].update(write_authorized="false"),
    lambda raw: raw["projects"]["youtube-auto"]["repositories"][YOUTUBE].update(target_branch="../main"),
])
def test_unknown_or_unsafe_profile_data_fails_closed(tmp_path, mutation):
    raw = _raw()
    mutation(raw)
    with pytest.raises((ValueError, TypeError)):
        project_profiles.load(_write(tmp_path, raw))


def test_duplicate_yaml_keys_and_repositories_fail_closed(tmp_path):
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(project_profiles.PROFILE_PATH.read_text().replace(
        "version: 1", "version: 1\nversion: 1", 1))
    with pytest.raises(ValueError, match="duplicate profile key"):
        project_profiles.load(duplicate)

    raw = _raw()
    raw["projects"]["second-company"] = {
        "business_unit": None,
        "repositories": {PILOT: deepcopy(raw["projects"]["company-os"]["repositories"][PILOT])},
    }
    with pytest.raises(ValueError, match="duplicate canonical repository"):
        project_profiles.load(_write(tmp_path, raw))


def test_task_profile_requires_exact_project_repository_and_business_unit():
    # The API project row is the identity source; display names and task text cannot choose a profile.
    from app import enums, models

    unit = models.BusinessUnit(key="youtube", name="YouTube", status=enums.BusinessUnitStatus.ACTIVE)
    project = models.Project(key="youtube-auto", name="YouTube automation pipeline",
                             business_unit=unit, repository=YOUTUBE, status=enums.ProjectStatus.ACTIVE)
    db = _db()
    db.add(project)
    db.flush()
    task = _task(db, key="TASK-9998", project_id=project.id,
                 context={"autonomous": True, "write": True, "stage_c": {"paths": ["tests/test_metrics.py"]}})
    profile = project_profiles.for_task(db, task)
    assert profile and profile["write_authorized"] is False
    assert stage_c_worker.admission(db, task) == "stage_c_repository_read_only"

    project.repository = None
    db.flush()
    assert project_profiles.for_task(db, task) is None
    assert stage_c_worker.admission(db, task) == "stage_c_repository_refused"

    project.repository = PILOT
    db.flush()
    assert project_profiles.for_task(db, task) is None


def test_profile_data_cannot_add_another_writer_without_runtime_support(tmp_path):
    raw = _raw()
    youtube = raw["projects"]["youtube-auto"]["repositories"][YOUTUBE]
    youtube.update(write_authorized=True, allowed_paths=["tests/test_metrics.py"],
                   execution_profile="sez-company")
    policy = project_profiles.load(_write(tmp_path, raw))
    assert project_profiles.current_executor(policy, PILOT, "sez-company") is None
