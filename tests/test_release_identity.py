"""Recovery requires four immutable/container identities and positive no-migration PostgreSQL evidence."""
import json
import subprocess

import pytest

from app import release_identity as identity

SHA = "a" * 40


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    rows = [{"Name": "/" + name, "Image": "sha256:" + str(i) * 64,
             "Config": {"Env": ["COMPANY_OS_GIT_SHA=" + SHA], "User": "10001"},
             "HostConfig": {"Privileged": False}, "Mounts": [],
             "NetworkSettings": {"Networks": {"company": {}}}, "State": {"Running": True}}
            for i, name in enumerate(identity.SERVICES.values(), 1)]
    state = {"rows": rows, "revision": "a" * 12, "schema": b"CREATE TABLE tasks(id bigint);"}

    def read(command, **kwargs):
        if command[1] == "inspect":
            return json.dumps(state["rows"]).encode()
        if "psql" in command:
            return state["revision"].encode()
        assert "pg_dump" in command
        return state["schema"]

    monkeypatch.setattr(identity, "read", read)
    monkeypatch.setattr(identity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    (tmp_path / "db.dump").write_bytes(b"readable disposable backup")
    identity.recovery_check("snapshot", tmp_path, SHA, "company", "company")
    return tmp_path, state


def test_verified_recovery_preserves_four_identities_and_database_schema(evidence):
    path, _ = evidence
    identity.recovery_check("verify", path, SHA, "company", "company")
    saved = json.loads((path / "recovery-identity.json").read_text())
    assert set(saved["images"]) == set(identity.SERVICES)
    assert "Env" not in saved and saved["revision"] == "a" * 12


@pytest.mark.parametrize("change", [
    lambda s: s["rows"][0]["HostConfig"].update(Privileged=True),
    lambda s: s["rows"][1]["Config"].update(User="0"),
    lambda s: s["rows"][2]["Mounts"].append({"Source": "/", "Destination": "/host"}),
    lambda s: s["rows"][3].update(Image="sha256:" + "f" * 64),
    lambda s: s.update(revision="b" * 12),
    lambda s: s.update(schema=b"CREATE TABLE tasks(id text);"),
    lambda s: s["rows"].pop(),
])
def test_recovery_refuses_config_image_database_or_inventory_drift(evidence, change):
    path, state = evidence
    change(state)
    with pytest.raises(ValueError):
        identity.recovery_check("verify", path, SHA, "company", "company")


def test_attempted_migration_never_becomes_verified_from_a_matching_revision(evidence):
    path, _ = evidence
    (path / "migration-attempted").write_text("a -> b")
    with pytest.raises(ValueError, match="manual database recovery"):
        identity.recovery_check("verify", path, SHA, "company", "company")


def test_missing_or_changed_backup_and_identity_are_not_recovery_evidence(evidence):
    path, _ = evidence
    (path / "db.dump").write_bytes(b"different backup")
    with pytest.raises(ValueError, match="backup identity differs"):
        identity.recovery_check("verify", path, SHA, "company", "company")


def test_schema_dump_random_guard_tokens_do_not_change_its_identity(evidence):
    path, state = evidence
    state["schema"] = b"\\restrict random\n" + state["schema"] + b"\n\\unrestrict random"
    identity.recovery_check("verify", path, SHA, "company", "company")


def test_migration_marker_is_durable_and_cannot_be_reused(tmp_path):
    identity.mark_migration(tmp_path, "a" * 12, "b" * 12)
    assert (tmp_path / "migration-attempted").read_text() == "a" * 12 + " " + "b" * 12 + "\n"
    with pytest.raises(FileExistsError):
        identity.mark_migration(tmp_path, "a" * 12, "b" * 12)


def test_legacy_compose_tag_is_preserved_as_recovery_evidence(tmp_path, monkeypatch):
    rows = [{"Name": "/" + name, "Image": "sha256:" + str(i) * 64,
             "Config": {"Image": "company-os-" + component + ":latest",
                        "Env": ["COMPANY_OS_GIT_SHA=" + SHA]}, "HostConfig": {}, "Mounts": [],
             "NetworkSettings": {"Networks": {}}, "State": {"Running": True}}
            for i, (component, name) in enumerate(identity.SERVICES.items(), 1)]
    monkeypatch.setattr(identity, "read", lambda args: json.dumps(rows).encode() if args[1] == "inspect" else
                        b"aaaaaaaaaaaa" if "psql" in args else b"CREATE TABLE tasks(id bigint);")
    assert identity.recovery_state(SHA, "u", "d")["compose_tag"] == "latest"
