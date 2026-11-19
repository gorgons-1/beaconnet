"""JSON-lines logging to stdout. Callers pass only safe fields - never bodies, keys or tokens."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
            "level": record.levelname,
            "service": self.service,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info).splitlines()[-1][:300]
        return json.dumps(out, default=str)


def setup(service: str, level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncssh"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: object) -> None:
    logger.log(level, name, extra={"fields": fields})
