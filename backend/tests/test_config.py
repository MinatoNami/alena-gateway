from __future__ import annotations

from pathlib import Path

import pytest

from app import config

from conftest import FIXTURE


def test_reads_the_registry(registry):
    assert [s.id for s in registry.services] == ["alpha", "beta"]
    assert registry.services[0].name == "Alpha"
    assert registry.services[0].prefix == "/alpha/"
    assert registry.services[0].port == 9001


def test_origin_loses_its_trailing_slash(registry):
    # Links are built as origin + prefix, and prefix already opens with a slash.
    # A trailing one here produces //alpha/, which some upstreams treat as a
    # different path than /alpha/.
    assert registry.origin == "https://gateway.example.ts.net"


def test_env_origin_beats_the_file(monkeypatch):
    # The tracked registry carries a placeholder precisely so a real hostname
    # never lands in a public repository. If the file ever won, the scrub would
    # be undone silently by a redeploy.
    monkeypatch.setenv("GATEWAY_ORIGIN", "https://real.example.ts.net")
    assert config.load(FIXTURE).origin == "https://real.example.ts.net"


def test_env_origin_also_loses_its_trailing_slash(monkeypatch):
    monkeypatch.setenv("GATEWAY_ORIGIN", "https://real.example.ts.net/")
    assert config.load(FIXTURE).origin == "https://real.example.ts.net"


def test_env_upstream_host_beats_the_file(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM_HOST", "127.0.0.1")
    assert config.load(FIXTURE).upstream_host == "127.0.0.1"


def test_components_are_nested_under_their_service(registry):
    alpha, beta = registry.services
    assert [c.id for c in alpha.components] == ["alpha-core"]
    assert beta.components == []


def test_a_component_can_be_unrouted(registry):
    alpha = registry.services[0]
    assert alpha.routed is True
    assert alpha.components[0].routed is False


def test_reserved_root_paths_are_read(registry):
    assert registry.services[0].reserves == ["/v1/", "/healthz"]
    assert registry.services[1].reserves == []


def test_health_url_is_built_from_the_probe_host(registry):
    assert registry.services[0].health_url("10.0.0.1") == "http://10.0.0.1:9001/healthz"


def test_health_path_defaults_to_root(tmp_path: Path):
    # `health:` is optional. Falling back to "/" is right for an app with no
    # health route — a 200 on the shell still proves the process is answering.
    path = tmp_path / "s.yaml"
    path.write_text("origin: https://x.example\nservices:\n  - id: a\n    port: 1\n")
    assert config.load(path).services[0].health_path == "/"


def test_an_empty_registry_is_a_startup_failure(tmp_path: Path):
    # The alternative is a page that renders nothing, which reads as "all is
    # well" rather than "this is misconfigured".
    path = tmp_path / "s.yaml"
    path.write_text("origin: https://x.example\nservices: []\n")
    with pytest.raises(ValueError, match="no services"):
        config.load(path)


def test_a_registry_with_no_services_key_is_a_startup_failure(tmp_path: Path):
    path = tmp_path / "s.yaml"
    path.write_text("origin: https://x.example\n")
    with pytest.raises(ValueError, match="no services"):
        config.load(path)
