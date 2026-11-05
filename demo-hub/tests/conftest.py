"""Shared test helpers.

``console_client`` is a TestClient that calls the hub as the console does: on the hub's own
loopback Host, with ``X-Pantry-Console: 1`` and JSON (guard.py refuses anything else that
changes something). Tests of the guard itself build requests by hand instead.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

CONSOLE_HEADERS = {"X-Pantry-Console": "1", "Content-Type": "application/json"}
HUB_BASE_URL = "http://127.0.0.1:8090"         # Settings.hub_port's default


def console_client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=HUB_BASE_URL, headers=CONSOLE_HEADERS)


@pytest.fixture
def console() -> Callable[[FastAPI], TestClient]:
    return console_client
