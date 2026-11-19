#!/usr/bin/env bash
# Regenerate shared/schemas/openapi.json from the bbs-core code (the API contract).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=python:3.12.8-slim-bookworm
docker run --rm -v "$PWD:/src:ro" -w /src --tmpfs /tmp:exec --user "$(id -u):$(id -g)" -e HOME=/tmp "$IMAGE" sh -c '
  python -m venv /tmp/venv >/dev/null &&
  /tmp/venv/bin/pip install -q --disable-pip-version-check --no-deps -r services/bbs-core/requirements.lock &&
  mkdir -p /tmp/t && cp -r config.example /tmp/t/config && head -c 48 /dev/urandom | base64 > /tmp/t/a && head -c 48 /dev/urandom | base64 > /tmp/t/b &&
  cd services/bbs-core && BBS_CONFIG_DIR=/tmp/t/config BBS_DB_PATH=/tmp/t/db BBS_SSH_GATEWAY_TOKEN_FILE=/tmp/t/a \
  BBS_ADMIN_GATEWAY_TOKEN_FILE=/tmp/t/b /tmp/venv/bin/python -m bbs_core.cli openapi' > shared/schemas/openapi.json.new
mv shared/schemas/openapi.json.new shared/schemas/openapi.json
echo "wrote shared/schemas/openapi.json"
