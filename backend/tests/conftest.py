from __future__ import annotations

import os
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "services.yaml"

# Set before anything imports app.main, which loads the registry at import time.
# A test run that reached the real /etc path would either fail on a developer
# machine or, worse, probe a live deployment.
os.environ.setdefault("GATEWAY_SERVICES_FILE", str(FIXTURE))
os.environ.pop("GATEWAY_ORIGIN", None)
os.environ.pop("GATEWAY_UPSTREAM_HOST", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import config  # noqa: E402


@pytest.fixture
def registry():
    return config.load(FIXTURE)


def responder(by_port):
    """Build a transport that answers per upstream port.

    Values are either an int status, or an exception to raise. Keyed by port —
    not by URL — so a test can say "this one times out and that one is fine"
    without restating the registry.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        outcome = by_port[request.url.port]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, text="ok")

    return httpx.MockTransport(handle)
