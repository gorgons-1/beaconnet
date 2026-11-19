#!/usr/bin/env bash
# Re-resolve services/*/requirements.lock from requirements.txt inside the pinned base image.
# Run after changing a requirements.txt; commit the new lock files; then rebuild.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=python:3.12.8-slim-bookworm
for svc in bbs-core bbs-ssh bbs-admin; do
  {
    echo "# Full pinned dependency set, resolved in $IMAGE from requirements.txt"
    echo "# Regenerate: scripts/lock-deps.sh"
    docker run --rm -v "$PWD/services/$svc/requirements.txt:/r.txt:ro" "$IMAGE" sh -c \
      'pip install -q --disable-pip-version-check --root-user-action=ignore -r /r.txt >/dev/null && pip freeze --exclude pip --exclude setuptools --exclude wheel'
  } > "services/$svc/requirements.lock.new"
  mv "services/$svc/requirements.lock.new" "services/$svc/requirements.lock"
  echo "locked $svc"
done
