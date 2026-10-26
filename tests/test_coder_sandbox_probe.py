"""Offline proof runner fails closed; real Docker proof is a separate explicit check."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sandbox_probe", Path(__file__).parents[1] / "scripts" / "check-coder-sandbox.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_unknown_or_failed_security_checks_never_pass():
    good = dict.fromkeys(probe.REQUIRED, True)
    probe.verify(good)
    with pytest.raises(ValueError):
        probe.verify({**good, 'untrusted_extra': 'must not be printed'})
    for key in probe.REQUIRED:
        for value in (False, None, 1, "true"):
            with pytest.raises(ValueError):
                probe.verify({**good, key: value})
        with pytest.raises(ValueError):
            probe.verify({k: v for k, v in good.items() if k != key})


def test_only_local_immutable_image_ids_are_accepted():
    probe.validate_image("sha256:" + "a" * 64)
    for image in ("latest", "python:3.12", "repo@sha256:" + "a" * 64, "sha256:abc", ""):
        with pytest.raises(ValueError):
            probe.validate_image(image)


def test_docker_profile_rejects_wider_mounts_network_or_permissions():
    good = {'Config': {'User': '10001:10001'}, 'Mounts': [
        {'Type': 'bind', 'Source': '/fixture', 'Destination': '/workspace', 'RW': True}],
        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
                       'CapDrop': ['ALL'], 'CapAdd': None, 'SecurityOpt': ['no-new-privileges:true'],
                       'Memory': 134217728, 'NanoCpus': 250000000, 'PidsLimit': 32,
                       'PidMode': '', 'IpcMode': 'private'}}
    probe.verify_profile(good, '/fixture')
    for key, value in [('NetworkMode', 'host'), ('ReadonlyRootfs', False),
                       ('Privileged', True), ('Memory', 0), ('PidsLimit', 0),
                       ('CapAdd', ['SYS_ADMIN']), ('PidMode', 'host')]:
        with pytest.raises(ValueError):
            probe.verify_profile({**good, 'HostConfig': {**good['HostConfig'], key: value}}, '/fixture')
    with pytest.raises(ValueError):
        probe.verify_profile({**good, 'Mounts': good['Mounts'] * 2}, '/fixture')
    with pytest.raises(ValueError):
        probe.verify_profile({}, '/fixture')
