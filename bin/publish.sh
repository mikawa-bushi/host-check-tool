#!/bin/bash
# Publish host-check-tool (procdiag) to PyPI.
# Prerequisites: `poetry config pypi-token.pypi <token>` (and test-pypi for a dry run).
set -euo pipefail

# Validate pyproject metadata before doing anything.
poetry check

# Regenerate the lock so a version bump is captured.
poetry lock
poetry install

# Build sdist + wheel into dist/.
poetry build

# Optional dry run against TestPyPI first:
#   poetry publish -r test-pypi
# Then the real thing:
poetry publish
