"""Shared guards: tests never read a real .env, native credential store or network."""

import pytest

from spending_optimiser.dotenv_store import DotenvFile


@pytest.fixture(autouse=True)
def default_env_is_off_limits(tmp_path_factory, monkeypatch):
    """Point the default .env somewhere unreadable-by-test so a stray read fails loudly."""
    forbidden = tmp_path_factory.mktemp("default-env") / ".env"
    monkeypatch.setenv("SPENDING_OPTIMISER_ENV", str(forbidden))
    original = DotenvFile._text

    def guarded(self):
        if self.path == forbidden:
            raise AssertionError("The default .env must not be read; inject a temporary file")
        return original(self)

    monkeypatch.setattr(DotenvFile, "_text", guarded)
