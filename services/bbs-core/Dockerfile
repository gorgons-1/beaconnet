# bbs-core: the only container that opens the SQLite database.
# Build context is the repository root (see docker-compose.yml).
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 bbs \
 && useradd --uid 10001 --gid bbs --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin bbs

WORKDIR /app
COPY services/bbs-core/requirements.lock /app/requirements.lock
RUN pip install --no-deps -r /app/requirements.lock && pip check

COPY migrations /app/migrations
COPY services/bbs-core/bbs_core /app/bbs_core
RUN python -m compileall -q /app/bbs_core \
 && mkdir -p /data /backups && chown bbs:bbs /data /backups && chmod 0750 /data

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "bbs_core.cli", "healthcheck"]
ENTRYPOINT ["python", "-m", "bbs_core.cli"]
CMD ["serve"]
