"""selftest release install-directory resolution regression (tool-w3 de-machine-binding; the machine-hardcoded default was removed in v0.9.0):
fallback chain = PGTUNER_RELEASE_BINDIR env > first release instance's bindir in the registry > error.
Only the resolution function is tested; the with-db part is not triggered (no real PG run)."""
from types import SimpleNamespace

import pytest

from pgtuner import selftest


def _fake_insts(*pairs):
    return [SimpleNamespace(name=n, build_type=b, bindir=d) for n, b, d in pairs]


def test_release_bindir_env_override(monkeypatch):
    monkeypatch.setenv("PGTUNER_RELEASE_BINDIR", "/opt/pg-release")
    assert selftest.release_bindir() == "/opt/pg-release"


def test_release_bindir_registry_fallback(monkeypatch):
    """Without the env variable, the bindir of the first release instance in the registry is used (debug instances do not count)."""
    monkeypatch.delenv("PGTUNER_RELEASE_BINDIR", raising=False)
    monkeypatch.setattr(selftest.instance_mod, "list_instances", lambda *a, **k: _fake_insts(
        ("dbg", "debug", "/fake/dbg-bin"), ("rel", "release", "/fake/rel-prefix")))
    assert selftest.release_bindir() == "/fake/rel-prefix"


def test_release_bindir_empty_env_falls_back(monkeypatch):
    """An empty string counts as unset; the registry fallback chain applies (the os.environ.get(...) or semantics)."""
    monkeypatch.setenv("PGTUNER_RELEASE_BINDIR", "")
    monkeypatch.setattr(selftest.instance_mod, "list_instances",
                        lambda *a, **k: _fake_insts(("rel", "release", "/fake/rel-prefix")))
    assert selftest.release_bindir() == "/fake/rel-prefix"


def test_release_bindir_no_candidate_raises(monkeypatch):
    """Registry empty (or debug-only): a clear error with repair guidance; no more machine-hardcoded fallback."""
    monkeypatch.delenv("PGTUNER_RELEASE_BINDIR", raising=False)
    monkeypatch.setattr(selftest.instance_mod, "list_instances", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="release"):
        selftest.release_bindir()
    monkeypatch.setattr(selftest.instance_mod, "list_instances",
                        lambda *a, **k: _fake_insts(("dbg", "debug", "/fake/dbg-bin")))
    with pytest.raises(RuntimeError, match="setup.sh"):
        selftest.release_bindir()
