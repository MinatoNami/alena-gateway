"""Health probing for everything in the registry.

Three things this deliberately does:

* **Probes concurrently.** A dead upstream costs the full timeout, and probing
  in sequence would make one dead service delay the report on every other.

* **Caches for a few seconds.** The page polls, and several open tabs multiply
  into upstream traffic. One probe cycle is shared by every caller that arrives
  inside the TTL window.

* **Collapses concurrent refreshes.** Without the lock, N requests arriving on a
  cold cache each start their own probe cycle — the stampede lands hardest at
  exactly the moment something has just gone down.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass

import httpx

from .config import Registry, Target

# Long enough for a Django worker that is busy rather than dead, short enough
# that the page does not sit spinning on a host that is off.
TIMEOUT_SECONDS = 4.0
CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class Result:
    id: str
    name: str
    status: str            # "up" | "down"
    http_status: int | None
    latency_ms: int | None
    detail: str | None
    port: int
    routed: bool
    url: str | None        # where a person should go, None if not routed
    blurb: str
    repo: str | None
    reserves: list[str]
    components: list[dict]


async def _probe_one(client: httpx.AsyncClient, target: Target, host: str) -> tuple[str, int | None, int | None, str | None]:
    """Return (status, http_status, latency_ms, detail) for one target."""
    url = target.health_url(host)
    started = time.perf_counter()
    try:
        # follow_redirects is off on purpose: a 301 from an app that has decided
        # it lives somewhere else is information, not something to chase.
        response = await client.get(url)
    except httpx.TimeoutException:
        return "down", None, int((time.perf_counter() - started) * 1000), f"no response in {TIMEOUT_SECONDS:g}s"
    except httpx.HTTPError as exc:
        # Connection refused is the ordinary case for a stopped container, and
        # httpx's own message for it is more useful than anything we'd write.
        return "down", None, None, type(exc).__name__ + (f": {exc}" if str(exc) else "")

    latency_ms = int((time.perf_counter() - started) * 1000)

    # 2xx and 3xx both mean the process is answering. A Nuxt app shell that
    # redirects an unauthenticated visitor to /login is up, not down.
    if response.status_code < 400:
        return "up", response.status_code, latency_ms, None
    return "down", response.status_code, latency_ms, f"HTTP {response.status_code}"


async def _probe_all(registry: Registry, transport: httpx.AsyncBaseTransport | None = None) -> list[Result]:
    # transport is a seam for the tests, which drive every branch of the
    # classification below without a network or a real upstream. Production
    # passes nothing and gets httpx's default.
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        # Flatten services and their components into one round of probes so a
        # slow component does not serialise behind its parent.
        flat: list[tuple[Target, Target | None]] = []
        for service in registry.services:
            flat.append((service, None))
            for component in service.components:
                flat.append((component, service))

        outcomes = await asyncio.gather(
            *(_probe_one(client, target, registry.upstream_host) for target, _ in flat)
        )

    by_id = {target.id: outcome for (target, _), outcome in zip(flat, outcomes)}

    results: list[Result] = []
    for service in registry.services:
        status, http_status, latency_ms, detail = by_id[service.id]
        results.append(
            Result(
                id=service.id,
                name=service.name,
                status=status,
                http_status=http_status,
                latency_ms=latency_ms,
                detail=detail,
                port=service.port,
                routed=service.routed,
                url=f"{registry.origin}{service.prefix}" if service.routed and service.prefix else None,
                blurb=service.blurb,
                repo=service.repo,
                reserves=service.reserves,
                components=[
                    {
                        "id": component.id,
                        "name": component.name,
                        "port": component.port,
                        "routed": component.routed,
                        "status": by_id[component.id][0],
                        "http_status": by_id[component.id][1],
                        "latency_ms": by_id[component.id][2],
                        "detail": by_id[component.id][3],
                    }
                    for component in service.components
                ],
            )
        )
    return results


class Prober:
    def __init__(self, registry: Registry, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._registry = registry
        self._transport = transport
        self._lock = asyncio.Lock()
        self._cached: list[Result] = []
        self._checked_at: float = 0.0
        # Counts probe cycles, not requests. The cache and the lock exist to
        # keep this number low when several tabs poll at once, and a test that
        # cannot see it can only assert on timing.
        self.cycles = 0

    async def status(self, *, force: bool = False) -> tuple[list[Result], float]:
        """Probe results and the wall-clock time they were taken."""
        if not force and self._cached and (time.time() - self._checked_at) < CACHE_TTL_SECONDS:
            return self._cached, self._checked_at

        async with self._lock:
            # Re-check inside the lock: whoever was holding it may have just
            # refreshed, in which case this caller wants that result, not
            # another round of probes.
            if not force and self._cached and (time.time() - self._checked_at) < CACHE_TTL_SECONDS:
                return self._cached, self._checked_at

            self._cached = await _probe_all(self._registry, self._transport)
            self._checked_at = time.time()
            self.cycles += 1
            return self._cached, self._checked_at


def to_json(results: list[Result]) -> list[dict]:
    return [asdict(result) for result in results]
