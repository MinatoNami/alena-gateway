"""Reads services.yaml into the shape the probes and the API want.

The file is read once at startup and cached. It is small, it changes only when
something is deployed, and re-reading it per request would put a disk hit in
front of a page that polls every few seconds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Bind-mounted in docker-compose.yml so the registry can be edited and the
# container restarted, without rebuilding the image.
CONFIG_PATH = Path(os.environ.get("GATEWAY_SERVICES_FILE", "/etc/alena-gateway/services.yaml"))


@dataclass(frozen=True)
class Target:
    """One thing that can be probed."""

    id: str
    name: str
    port: int
    health_path: str
    # False for upstreams that exist but are deliberately not reachable through
    # the gateway — athena's core API is published for diagnostics only.
    routed: bool = True
    blurb: str = ""
    prefix: str | None = None
    repo: str | None = None
    reserves: list[str] = field(default_factory=list)
    # Sub-processes reported underneath a service rather than beside it.
    components: list["Target"] = field(default_factory=list)

    def health_url(self, host: str) -> str:
        return f"http://{host}:{self.port}{self.health_path}"


@dataclass(frozen=True)
class Registry:
    origin: str
    upstream_host: str
    services: list[Target]


def _target(raw: dict, *, default_health: str = "/") -> Target:
    return Target(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        port=int(raw["port"]),
        health_path=raw.get("health", default_health),
        routed=bool(raw.get("routed", True)),
        blurb=raw.get("blurb", ""),
        prefix=raw.get("prefix"),
        repo=raw.get("repo"),
        reserves=list(raw.get("reserves", [])),
        components=[_target(c) for c in raw.get("components", [])],
    )


def load(path: Path | None = None) -> Registry:
    path = path or CONFIG_PATH
    raw = yaml.safe_load(path.read_text())

    # An empty or malformed registry would render an empty page that looks like
    # "everything is fine" rather than "the page is broken". Fail at startup.
    services = raw.get("services") or []
    if not services:
        raise ValueError(f"{path}: no services defined")

    return Registry(
        # The file's value is a placeholder; the deployment supplies the real
        # one. Reversing this precedence would put a specific machine's name in
        # a tracked file, which is how it leaks into a public repository.
        origin=(os.environ.get("GATEWAY_ORIGIN") or raw["origin"]).rstrip("/"),
        upstream_host=os.environ.get("GATEWAY_UPSTREAM_HOST") or raw.get("upstream_host", "host.docker.internal"),
        services=[_target(s) for s in services],
    )
