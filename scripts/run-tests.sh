#!/usr/bin/env bash
# Run the automated test suite in the same pinned Python image the services use.
# Works on the Pi or any Linux box with Docker. Extra args go to pytest, e.g. -k mail -x
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=python:3.12.8-slim-bookworm
exec docker run --rm -t -v "$PWD:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  --tmpfs /tmp:exec --user "$(id -u):$(id -g)" -e HOME=/tmp -e USER=tester "$IMAGE" sh -c '
    python -m venv /tmp/venv >/dev/null &&
    /tmp/venv/bin/pip install -q --disable-pip-version-check --no-deps \
      -r services/bbs-core/requirements.lock -r services/bbs-ssh/requirements.lock &&
    /tmp/venv/bin/pip install -q --disable-pip-version-check -r tests/requirements.txt &&
    /tmp/venv/bin/python -m pytest -p no:cacheprovider "$@"' -- "$@"
