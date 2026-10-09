"""Query-string values that must never reach a log: the OAuth callback's ``code``, ``state`` and
``error``, and any token. uvicorn's access log writes every request's path with its query to
``~/.pantry-demo/logs/hub.log``; ``RedactQuery`` masks those values in each record first.

``app.main()`` installs it on the ``uvicorn.access`` logger after uvicorn has configured its
logging. A hub started some other way (``uvicorn demo_hub.app:...``) does not get it.
"""

from __future__ import annotations

import logging
import re

SECRET_PARAMS = ("code", "state", "error", "error_description", "token", "access_token",
                 "refresh_token", "id_token", "client_secret", "code_verifier")
_PATTERN = re.compile(r"(?i)([?&](?:" + "|".join(SECRET_PARAMS) + r")=)[^&#\s\"']*")


def redact_query(text: str) -> str:
    return _PATTERN.sub(r"\1***", text)


class RedactQuery(logging.Filter):
    """Masks secret query values in a record's arguments and message; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_query(a) if isinstance(a, str) else a
                                for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_query(v) if isinstance(v, str) else v
                           for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = redact_query(record.msg)
        return True


def install(logger_name: str = "uvicorn.access") -> RedactQuery:
    logger = logging.getLogger(logger_name)
    for f in logger.filters:
        if isinstance(f, RedactQuery):
            return f
    f = RedactQuery()
    logger.addFilter(f)
    return f
