"""Fail-closed project/repository identities and Stage C execution scopes."""
import re
from pathlib import Path

import yaml

from app import coder_source, models, repo_guardian, repo_guardian_github

PROFILE_PATH = Path(__file__).resolve().parents[1] / "agents/stage_c_profiles.yaml"
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EXECUTION_PROFILES = {"sez-company"}


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise ValueError("duplicate profile key")
            keys.add(key)
        return super().construct_mapping(node, deep)


def _mapping(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{label} has missing or unknown fields")
    return value


def load(path: Path | str | None = None) -> dict:
    """Load exact project/repository identities; malformed or ambiguous policy denies work."""
    raw = yaml.load(Path(path or PROFILE_PATH).read_text(), Loader=_UniqueSafeLoader)
    _mapping(raw, {"version", "projects"}, "profile registry")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("profile registry requires version 1")
    if not isinstance(raw["projects"], dict) or not raw["projects"]:
        raise ValueError("profile registry requires projects")

    projects, repositories = {}, {}
    for project_key, project in raw["projects"].items():
        if not isinstance(project_key, str) or not KEY.fullmatch(project_key):
            raise ValueError("invalid project key")
        _mapping(project, {"business_unit", "repositories"}, f"project {project_key}")
        unit = project["business_unit"]
        if unit is not None and (not isinstance(unit, str) or not KEY.fullmatch(unit)):
            raise ValueError(f"{project_key}: invalid business unit")
        if not isinstance(project["repositories"], dict):
            raise ValueError(f"{project_key}: repositories must be a mapping")

        project_repos = {}
        for name, item in project["repositories"].items():
            if (not isinstance(name, str) or repo_guardian.repository_name(name) != name
                    or name in repositories):
                raise ValueError(f"{project_key}: invalid or duplicate canonical repository")
            repo = _mapping(item, {"target_branch", "write_authorized", "allowed_paths", "execution_profile"},
                            f"repository {name}")
            branch, write, paths, execution = (repo["target_branch"], repo["write_authorized"],
                                                repo["allowed_paths"], repo["execution_profile"])
            if not isinstance(branch, str) or not repo_guardian_github.BRANCH.fullmatch(branch):
                raise ValueError(f"{name}: invalid target branch")
            if type(write) is not bool:
                raise ValueError(f"{name}: write_authorized must be a boolean")
            if (not isinstance(paths, list) or len(paths) > 20
                    or any(not isinstance(path, str) for path in paths)
                    or len(set(paths)) != len(paths)
                    or not all(coder_source._safe_path(path) for path in paths)):
                raise ValueError(f"{name}: unsafe or unbounded allowed paths")
            if write:
                if not paths or not isinstance(execution, str) or not KEY.fullmatch(execution):
                    raise ValueError(f"{name}: writable profile needs paths and an execution profile")
            elif paths or execution is not None:
                raise ValueError(f"{name}: read-only profile cannot carry paths or an execution profile")
            profile = {**repo, "project_key": project_key, "business_unit": unit,
                       "repository": name}
            project_repos[name] = profile
            repositories[name] = profile
        projects[project_key] = {"key": project_key, "business_unit": unit,
                                 "repositories": project_repos}

    return {"projects": projects, "repositories": repositories}


def for_project(policy: dict, project_key: str | None, repository: str | None = None) -> dict | None:
    project = policy.get("projects", {}).get(project_key)
    if project is None:
        return None
    return project if repository is None else project["repositories"].get(repository)


def for_task(db, task, policy: dict | None = None) -> dict | None:
    project = db.get(models.Project, task.project_id) if task.project_id else None
    if project is None:
        return None
    unit = db.get(models.BusinessUnit, project.business_unit_id) if project.business_unit_id else None
    repository = repo_guardian.repository_name(project.repository)
    if repository is None:
        return None
    profile = for_project(policy or load(), project.key, repository)
    if profile is None or profile["business_unit"] != (unit.key if unit else None):
        return None
    return profile


def writable_repositories(policy: dict) -> list[str]:
    return sorted(name for name, profile in policy["repositories"].items()
                  if profile["write_authorized"])


def current_executor(policy: dict, repository: str, execution_profile: str) -> dict | None:
    """The current runner supports one explicitly named profile; expansion needs runtime support."""
    profile = for_project(policy, "company-os", repository)
    if (writable_repositories(policy) != [repository] or profile is None
            or not profile["write_authorized"] or profile["execution_profile"] != execution_profile
            or execution_profile not in EXECUTION_PROFILES):
        return None
    return profile
