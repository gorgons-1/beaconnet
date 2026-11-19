# bbs-admin: SSH terminal gateway. No database access; talks to bbs-core over the internal API.
# Build context is the repository root (see docker-compose.yml).
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BBS_LISTEN_PORT=2223 BBS_SERVICE_NAME=bbs-admin

RUN groupadd --gid 10001 bbs \
 && useradd --uid 10001 --gid bbs --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin bbs

WORKDIR /app
COPY services/bbs-admin/requirements.lock /app/requirements.lock
RUN pip install --no-deps -r /app/requirements.lock && pip check

COPY shared/bbs_gateway /app/bbs_gateway
COPY services/bbs-admin/bbs_admin /app/bbs_admin
RUN python -m compileall -q /app/bbs_gateway /app/bbs_admin

USER 10001:10001
EXPOSE 2223
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-m", "bbs_gateway.healthcheck"]
CMD ["python", "-m", "bbs_admin"]
