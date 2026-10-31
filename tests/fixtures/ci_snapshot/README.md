Byte-for-byte copies of `.github/workflows/*.yml`, kept in the shipped tree so the release reviewer (whose packet
covers only the deployed paths) can read exactly what CI runs. `tests/test_ci_snapshot.py` fails if a copy drifts
from the real workflow, if a workflow is added or removed without updating this directory, or if a CircleCI
configuration or the retired Jules review workflow reappears.
