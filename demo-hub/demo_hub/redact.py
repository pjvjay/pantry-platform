"""Query-string values that must never reach a log: the OAuth callback's ``code``, ``state`` and
``error``, any token, and ``q``, the address text of ``/hub/geocode``. uvicorn's access log
writes every request's path with its query to ``~/.pantry-demo/logs/hub.log``, and httpx logs
each request it makes with its URL; ``RedactQuery`` masks those values in each record first.

``app.main()`` installs it on the ``uvicorn.access`` logger after uvicorn has configured its
logging; the geocoder installs it on ``httpx``. A hub started some other way
(``uvicorn demo_hub.app:...``) does not get the first.
"""

from __future__ import annotations

import logging
import re
from typing import Any

SECRET_PARAMS = ("code", "state", "error", "error_description", "token", "access_token",
                 "refresh_token", "id_token", "client_secret", "code_verifier", "q")
_PATTERN = re.compile(r"(?i)([?&](?:" + "|".join(SECRET_PARAMS) + r")=)[^&#\s\"']*")


def redact_query(text: str) -> str:
    return _PATTERN.sub(r"\1***", text)


def _clean(arg: Any) -> Any:
    """A log argument with its secrets masked. An object that is not text (httpx logs its URL
    as one) becomes the masked text only when something was masked."""
    if isinstance(arg, str):
        return redact_query(arg)
    if arg is None or isinstance(arg, int | float):
        return arg
    text = str(arg)
    masked = redact_query(text)
    return masked if masked != text else arg


class RedactQuery(logging.Filter):
    """Masks secret query values in a record's arguments and message; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_clean(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _clean(v) for k, v in record.args.items()}
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
