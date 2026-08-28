from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import responder

from app import probes
from app.probes import Prober

ALPHA, ALPHA_CORE, BETA = 9001, 9002, 9003


def by_id(results):
    return {r.id: r for r in results}


async def probe(registry, by_port):
    return by_id(await probes._probe_all(registry, responder(by_port)))


# --- classification ----------------------------------------------------------

@pytest.mark.parametrize("status", [200, 204])
async def test_2xx_is_up(registry, status):
    out = await probe(registry, {ALPHA: status, ALPHA_CORE: 200, BETA: 200})
    assert out["alpha"].status == "up"
    assert out["alpha"].http_status == status


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_3xx_is_up(registry, status):
    # This is the one that matters. Both Nuxt apps behind this gateway redirect
    # an unauthenticated visitor to their login page, so treating a redirect as
    # a failure would have reported two healthy services down permanently.
    out = await probe(registry, {ALPHA: status, ALPHA_CORE: 200, BETA: 200})
    assert out["alpha"].status == "up"


@pytest.mark.parametrize("status", [400, 401, 404, 500, 502])
async def test_4xx_and_5xx_are_down(registry, status):
    out = await probe(registry, {ALPHA: status, ALPHA_CORE: 200, BETA: 200})
    assert out["alpha"].status == "down"
    assert out["alpha"].detail == f"HTTP {status}"


async def test_timeout_is_down_and_says_so(registry):
    out = await probe(
        registry,
        {ALPHA: httpx.ConnectTimeout("slow"), ALPHA_CORE: 200, BETA: 200},
    )
    assert out["alpha"].status == "down"
    assert "no response" in out["alpha"].detail
    assert out["alpha"].http_status is None


async def test_connection_refused_is_down(registry):
    # The ordinary case for a stopped container, and the one the first real
    # deploy hit when the prober could not reach the host's loopback.
    out = await probe(
        registry,
        {ALPHA: httpx.ConnectError("refused"), ALPHA_CORE: 200, BETA: 200},
    )
    assert out["alpha"].status == "down"
    assert "ConnectError" in out["alpha"].detail


async def test_latency_is_recorded_when_up(registry):
    out = await probe(registry, {ALPHA: 200, ALPHA_CORE: 200, BETA: 200})
    assert out["alpha"].latency_ms >= 0


# --- shape -------------------------------------------------------------------

async def test_a_routed_service_gets_a_link(registry):
    out = await probe(registry, {ALPHA: 200, ALPHA_CORE: 200, BETA: 200})
    assert out["alpha"].url == "https://gateway.example.ts.net/alpha/"


async def test_components_ride_on_their_parent(registry):
    out = await probe(registry, {ALPHA: 200, ALPHA_CORE: 500, BETA: 200})
    assert [c["id"] for c in out["alpha"].components] == ["alpha-core"]
    assert out["alpha"].components[0]["status"] == "down"
    # A sick component must not drag the parent down: the dashboard being up
    # while its core API is not is exactly the distinction worth seeing.
    assert out["alpha"].status == "up"


async def test_an_unrouted_component_is_reported_without_a_link(registry):
    out = await probe(registry, {ALPHA: 200, ALPHA_CORE: 200, BETA: 200})
    assert out["alpha"].components[0]["routed"] is False


async def test_one_dead_service_does_not_hide_the_others(registry):
    out = await probe(
        registry,
        {ALPHA: httpx.ConnectError("x"), ALPHA_CORE: 200, BETA: 200},
    )
    assert out["alpha"].status == "down"
    assert out["beta"].status == "up"


# --- caching -----------------------------------------------------------------

async def test_results_are_cached_within_the_ttl(registry):
    prober = Prober(registry, responder({ALPHA: 200, ALPHA_CORE: 200, BETA: 200}))
    await prober.status()
    await prober.status()
    assert prober.cycles == 1


async def test_force_bypasses_the_cache(registry):
    prober = Prober(registry, responder({ALPHA: 200, ALPHA_CORE: 200, BETA: 200}))
    await prober.status()
    await prober.status(force=True)
    assert prober.cycles == 2


async def test_the_cache_expires(registry, monkeypatch):
    monkeypatch.setattr(probes, "CACHE_TTL_SECONDS", 0)
    prober = Prober(registry, responder({ALPHA: 200, ALPHA_CORE: 200, BETA: 200}))
    await prober.status()
    await prober.status()
    assert prober.cycles == 2


async def test_concurrent_callers_share_one_probe_cycle(registry):
    # Several open tabs polling a cold cache must not each start their own
    # round of probes — the stampede would land hardest at the moment something
    # has just gone down and every probe is sitting out its full timeout.
    prober = Prober(registry, responder({ALPHA: 200, ALPHA_CORE: 200, BETA: 200}))
    await asyncio.gather(*(prober.status() for _ in range(10)))
    assert prober.cycles == 1


async def test_the_timestamp_is_the_probe_time_not_the_request_time(registry):
    prober = Prober(registry, responder({ALPHA: 200, ALPHA_CORE: 200, BETA: 200}))
    _, first = await prober.status()
    _, second = await prober.status()
    assert first == second
