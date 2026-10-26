"""Shared guards: tests never read a real .env, native credential store or network."""

import shutil
from pathlib import Path

import pytest

from spending_optimiser import dotenv_store
from spending_optimiser.dotenv_store import DotenvFile

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def synthetic_application(root):
    """A copy of this application's identity files, so the default .env lands in `root`."""
    (root / "src" / "spending_optimiser").mkdir(parents=True)
    for name in ("pyproject.toml", ".gitignore"):
        shutil.copyfile(SOURCE_ROOT / name, root / name)
    return root / "src" / "spending_optimiser"


@pytest.fixture(autouse=True)
def default_env_is_off_limits(tmp_path_factory, monkeypatch):
    """Point the default .env somewhere unreadable-by-test so a stray read fails loudly."""
    root = tmp_path_factory.mktemp("default-app")
    monkeypatch.setattr(dotenv_store, "PACKAGE", synthetic_application(root))
    forbidden = root / ".env"
    original = DotenvFile._text

    def guarded(self):
        if self.path == forbidden:
            raise AssertionError("The default .env must not be read; inject a temporary file")
        return original(self)

    monkeypatch.setattr(DotenvFile, "_text", guarded)


@pytest.fixture
def application(tmp_path, monkeypatch):
    """A writable synthetic application root used as the default .env location."""
    root = tmp_path / "application"
    monkeypatch.setattr(dotenv_store, "PACKAGE", synthetic_application(root))
    return root
