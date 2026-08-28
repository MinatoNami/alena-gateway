from __future__ import annotations

import httpx
import pytest
from conftest import responder
from fastapi.testclient import TestClient

from app import main

ALPHA, ALPHA_CORE, BETA = 9001, 9002, 9003


@pytest.fixture
def client(monkeypatch):
    # Swap the module's prober for one that answers from a transport, so these
    # exercise the HTTP surface without reaching for a network.
    monkeypatch.setattr(
        main,
        "prober",
        main.Prober(main.registry, responder({ALPHA: 200, ALPHA_CORE: 200, BETA: 302})),
    )
    with TestClient(main.app) as c:
        yield c


def test_healthz_is_liveness_for_this_container_only(client):
    # Deliberately not a roll-up of the upstreams: a check that failed when a
    # different application was down would have docker restart the gateway,
    # taking the status page offline exactly when it is worth reading.
    body = client.get("/api/healthz").json()
    assert body["status"] == "ok"


def test_status_reports_every_service(client):
    body = client.get("/api/status").json()
    assert [s["id"] for s in body["services"]] == ["alpha", "beta"]
    assert body["origin"] == "https://gateway.example.ts.net"
    assert body["gateway"]["uptime_seconds"] >= 0


def test_status_is_never_cached(client):
    # The page is a live view. A cached copy is worse than useless, because it
    # looks current.
    assert client.get("/api/status").headers["cache-control"] == "no-store"


def test_status_carries_the_links_the_page_renders(client):
    body = client.get("/api/status").json()
    alpha = body["services"][0]
    assert alpha["url"] == "https://gateway.example.ts.net/alpha/"
    assert alpha["reserves"] == ["/v1/", "/healthz"]


def test_the_root_serves_the_status_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "alena-server" in response.text


def test_static_assets_are_served(client):
    for path in ("/styles.css", "/app.js"):
        assert client.get(path).status_code == 200


def test_an_unknown_page_renders_the_status_page_with_404(client):
    # A wrong path at this origin is usually someone reaching for one of the
    # applications, and the page lists all of them. The code stays 404 so it
    # does not become a soft 200 that tells a crawler the page exists.
    response = client.get("/athna", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Service status" in response.text


def test_an_unknown_api_path_stays_json(client):
    # A client calling /api wants something parseable, not a page.
    response = client.get("/api/nope", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_a_non_browser_404_stays_json(client):
    response = client.get("/favicon.ico", headers={"accept": "image/*"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_a_redirecting_upstream_is_reported_up(client):
    # beta answers 302 in this fixture, the way both Nuxt apps do for an
    # unauthenticated visitor.
    body = client.get("/api/status").json()
    beta = next(s for s in body["services"] if s["id"] == "beta")
    assert beta["status"] == "up"


def test_a_dead_upstream_surfaces_its_reason(monkeypatch):
    monkeypatch.setattr(
        main,
        "prober",
        main.Prober(
            main.registry,
            responder({ALPHA: httpx.ConnectError("refused"), ALPHA_CORE: 200, BETA: 200}),
        ),
    )
    with TestClient(main.app) as c:
        alpha = c.get("/api/status").json()["services"][0]
    assert alpha["status"] == "down"
    assert "ConnectError" in alpha["detail"]
