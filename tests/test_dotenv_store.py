"""Synthetic .env persistence: escaping, protection, atomicity, isolation. No real secrets."""

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values

from spending_optimiser import web
from spending_optimiser.dotenv_store import MAX_FILE, DotenvError, DotenvFile
from spending_optimiser.gmail_auth import (
    AUTHORIZATION_PREFIX,
    CLIENT_ID_NAME,
    CLIENT_SECRET_NAME,
    EnvStore,
    GmailProblem,
    configure_google,
)
from spending_optimiser.gmail_service import GmailService

CLIENT = json.dumps(
    {"installed": {"client_id": "synthetic.apps.googleusercontent.com", "client_secret": "s3cret"}}
)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def key(name):
    return "connection-" + hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def env(tmp_path):
    return DotenvFile(tmp_path / ".env")


def test_values_round_trip_through_a_real_parser_and_survive_restart(env):
    awkward = {
        "SYNTHETIC_QUOTES": """it's "quoted" and 'single'""",
        "SYNTHETIC_BACKSLASH": "C:\\path\\\\with\\n-not-a-newline\\'",
        "SYNTHETIC_SHELL": "$(touch pwned) `touch pwned` ${HOME} $HOME; rm -rf /",
        "SYNTHETIC_COMMENT": "value # not a comment",
        "SYNTHETIC_UNICODE": "café \u2713 \U0001f512",
        "SYNTHETIC_EQUALS": "a=b=c ",
        "SYNTHETIC_EMPTY": "",
    }
    env.update(awkward)
    assert DotenvFile(env.path).values(awkward) == awkward  # a fresh object, as after a restart
    # A third-party loader agrees without interpolation, so nothing is expanded or executed.
    assert dict(dotenv_values(env.path, interpolate=False)) == awkward
    assert not (env.path.parent / "pwned").exists()


def test_process_environment_is_never_touched(env, monkeypatch):
    monkeypatch.delenv("SYNTHETIC_TOKEN", raising=False)
    before = dict(os.environ)
    env.update({"SYNTHETIC_TOKEN": "fake-token"})
    assert env.values(["SYNTHETIC_TOKEN"]) == {"SYNTHETIC_TOKEN": "fake-token"}
    assert dict(os.environ) == before


def test_unrelated_settings_comments_and_layout_are_preserved_byte_for_byte(env):
    original = (
        "\ufeff# Hand-written settings\n"
        "\n"
        "UNRELATED='keep me'\n"
        "export EXPORTED=plain # trailing\r\n"
        "   INDENTED = spaced\n"
        "this line is not a binding\n"
        "SYNTHETIC_TOKEN=old\n"
        "SYNTHETIC_TOKEN=older\n"
        "UNRELATED_TAIL=end"
    )
    env.path.write_bytes(original.encode())
    env.update({"SYNTHETIC_TOKEN": "new", "SYNTHETIC_ADDED": "added"})
    updated = env.path.read_bytes().decode("utf-8")  # text mode would fold \r\n
    assert updated.startswith("\ufeff# Hand-written settings\n\nUNRELATED='keep me'\n")
    assert "export EXPORTED=plain # trailing\r\n   INDENTED = spaced\nthis line is" in updated
    assert updated.count("SYNTHETIC_TOKEN") == 1  # duplicates of a managed name collapse
    assert "UNRELATED_TAIL=end\n" in updated  # unterminated last line gains its newline
    assert env.values(["SYNTHETIC_TOKEN", "SYNTHETIC_ADDED"]) == {
        "SYNTHETIC_TOKEN": "new",
        "SYNTHETIC_ADDED": "added",
    }
    env.update(remove=["SYNTHETIC_TOKEN", "SYNTHETIC_ADDED"])
    remaining = dotenv_values(env.path, interpolate=False)
    assert dict(remaining) == {
        "UNRELATED": "keep me",
        "EXPORTED": "plain",
        "INDENTED": "spaced",
        "UNRELATED_TAIL": "end",
    }
    assert "this line is not a binding" in env.path.read_text(encoding="utf-8")


def test_exported_binding_keeps_its_export_prefix(env):
    env.path.write_text("export SYNTHETIC_TOKEN=old\n")
    env.update({"SYNTHETIC_TOKEN": "new"})
    assert env.path.read_text() == "export SYNTHETIC_TOKEN='new'\n"


def test_new_file_is_owner_only_and_loose_files_are_tightened(env):
    env.update({"SYNTHETIC_TOKEN": "fake"})
    assert mode(env.path) == 0o600
    env.path.chmod(0o644)
    assert env.values(["SYNTHETIC_TOKEN"])  # reading a hand-edited file tightens it
    assert mode(env.path) == 0o600
    env.path.chmod(0o666)
    env.update({"SYNTHETIC_OTHER": "fake"})
    assert mode(env.path) == 0o600
    assert not [p for p in env.path.parent.iterdir() if p.name.startswith(".env.")]


def test_symlinks_directories_and_foreign_paths_are_refused(env, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("SYNTHETIC_TOKEN=elsewhere\n")
    env.path.symlink_to(target)
    for action in (lambda: env.values(["SYNTHETIC_TOKEN"]), lambda: env.update({"A": "b"})):
        with pytest.raises(DotenvError) as error:
            action()
        assert error.value.reason == "unsafe_path"
    assert target.read_text() == "SYNTHETIC_TOKEN=elsewhere\n"
    directory = DotenvFile(tmp_path / "folder")
    (tmp_path / "folder").mkdir()
    with pytest.raises(DotenvError, match="unsafe_path"):
        directory.values(["A"])
    with pytest.raises(DotenvError, match="write_failed"):
        DotenvFile(tmp_path / "missing" / ".env").update({"SYNTHETIC_TOKEN": "x"})


def test_oversized_malformed_and_unsafe_input_is_refused_without_changes(env):
    env.update({"SYNTHETIC_TOKEN": "safe"})
    before = env.path.read_bytes()
    for bad in ("x" * 20_000, "line\nbreak", "nul\x00", "tab\tcontrol", "\x7f"):
        with pytest.raises(DotenvError, match="invalid_value"):
            env.update({"SYNTHETIC_TOKEN": bad})
    for name in ("lowercase", "1LEADING_DIGIT", "HAS SPACE", "HAS=EQUALS", "A" * 200, ""):
        with pytest.raises(DotenvError, match="invalid_name"):
            env.update({name: "x"})
    with pytest.raises(DotenvError, match="invalid_name"):
        env.update({f"A{i}": "x" for i in range(9)})
    env.path.write_bytes(b"A=" + b"x" * MAX_FILE)
    with pytest.raises(DotenvError, match="too_large"):
        env.values(["A"])
    env.path.write_bytes(b"\xff\xfe=not utf-8\n")
    with pytest.raises(DotenvError, match="unreadable"):
        env.values(["A"])
    env.path.write_bytes(before)
    assert env.values(["SYNTHETIC_TOKEN"]) == {"SYNTHETIC_TOKEN": "safe"}


@pytest.mark.parametrize("failure", ["replace", "fsync", "write"])
def test_failed_update_leaves_existing_file_and_directory_unchanged(env, monkeypatch, failure):
    env.update({"SYNTHETIC_TOKEN": "original", "KEEP": "me"})
    before = env.path.read_bytes()
    listing = sorted(os.listdir(env.path.parent))
    boom = OSError("injected")
    if failure == "replace":
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(boom))
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", lambda *a: (_ for _ in ()).throw(boom))
    else:
        real = os.fdopen

        class Broken:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()

            def write(self, data):
                raise boom

        monkeypatch.setattr(
            os,
            "fdopen",
            lambda fd, mode="r", *a, **k: (
                Broken(real(fd, mode, *a, **k)) if "w" in mode else real(fd, mode, *a, **k)
            ),
        )
    with pytest.raises(DotenvError, match="write_failed") as error:
        env.update({"SYNTHETIC_TOKEN": "replacement"})
    assert "replacement" not in str(error.value)
    assert env.path.read_bytes() == before
    assert sorted(os.listdir(env.path.parent)) == listing


def test_idempotent_removal_does_not_create_a_file(env):
    env.update(remove=["SYNTHETIC_TOKEN"])
    assert not env.path.exists()
    env.update({"SYNTHETIC_TOKEN": "x"})
    env.update(remove=["SYNTHETIC_TOKEN"])
    env.update(remove=["SYNTHETIC_TOKEN"])
    assert env.values(["SYNTHETIC_TOKEN"]) == {}


def test_env_store_keys_clients_and_authorizations_by_ledger(env):
    store = EnvStore(env)
    assert store.get("desktop-client") is None
    configure_google(store, CLIENT)
    assert json.loads(store.get("desktop-client"))["installed"] == {
        "client_id": "synthetic.apps.googleusercontent.com",
        "client_secret": "s3cret",
    }
    values = dotenv_values(env.path, interpolate=False)
    assert values[CLIENT_ID_NAME] == "synthetic.apps.googleusercontent.com"
    assert values[CLIENT_SECRET_NAME] == "s3cret"
    a, b = key("ledger-a"), key("ledger-b")
    store.put(a, json.dumps({"refresh_token": "token-a"}))
    store.put(b, json.dumps({"refresh_token": "token-b"}))
    assert json.loads(store.get(a))["refresh_token"] == "token-a"
    assert json.loads(store.get(b))["refresh_token"] == "token-b"
    store.delete(a)
    store.delete(a)  # idempotent
    assert store.get(a) is None and json.loads(store.get(b))["refresh_token"] == "token-b"
    assert store.get("desktop-client") is not None  # disconnect keeps the app's client settings
    assert sorted(dotenv_values(env.path)) == sorted(
        [
            CLIENT_ID_NAME,
            CLIENT_SECRET_NAME,
            AUTHORIZATION_PREFIX + b.removeprefix("connection-").upper(),
        ]
    )
    with pytest.raises(ValueError):
        store.put("connection-not-a-digest", "x")


def test_half_configured_client_is_not_configured_and_failures_are_redacted(env):
    env.update({CLIENT_ID_NAME: "synthetic.apps.googleusercontent.com"})
    assert EnvStore(env).get("desktop-client") is None
    env.update({CLIENT_ID_NAME: "", CLIENT_SECRET_NAME: ""})  # blank .env.example placeholders
    assert EnvStore(env).get("desktop-client") is None
    env.path.chmod(0o600)
    env.path.write_bytes(b"\xff")
    with pytest.raises(GmailProblem) as error:
        EnvStore(env).get("desktop-client")
    assert error.value.code == "env_unavailable"
    assert "\\xff" not in str(error.value)


def test_service_disconnect_and_ledgers_are_isolated_in_a_shared_env(tmp_path, env):
    store = EnvStore(env)
    configure_google(store, CLIENT)
    env.update({"UNRELATED_SETTING": "kept"})
    first = GmailService(tmp_path / "one.sqlite3", store=store)
    second = GmailService(tmp_path / "two.sqlite3", store=store)
    assert first.key != second.key
    store.put(first.key, json.dumps({"refresh_token": "one"}))
    store.put(second.key, json.dumps({"refresh_token": "two"}))
    first.connection = SimpleNamespace()
    first.disconnect()
    assert first.connection is None and store.get(first.key) is None
    assert json.loads(store.get(second.key))["refresh_token"] == "two"
    assert env.values(["UNRELATED_SETTING"]) == {"UNRELATED_SETTING": "kept"}
    assert json.loads(store.get("desktop-client"))["installed"]["client_secret"] == "s3cret"


def test_secrets_never_reach_arguments_urls_or_the_gpt_child_environment(tmp_path, monkeypatch):
    from spending_optimiser import codex_client as module

    store = EnvStore(DotenvFile(tmp_path / ".env"))
    configure_google(store, CLIENT)
    monkeypatch.setenv(CLIENT_SECRET_NAME, "s3cret")  # even an exported variable stays out
    binary = tmp_path / "dummy-official-binary"
    binary.write_bytes(b"synthetic binary, never executed")
    monkeypatch.setattr(module, "ARM64_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest())
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    captured = {}

    class Process:
        stdin, stdout = io.BytesIO(), io.BytesIO()

        def terminate(self):
            pass

        def wait(self, **kwargs):
            pass

    def spawn(args, **kwargs):
        captured.update(args=args, **kwargs)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    native = module.CodexClient(binary, tmp_path / "isolated")
    monkeypatch.setattr(native.selector, "register", lambda *args: None)
    monkeypatch.setattr(native, "request", lambda *args: {})
    monkeypatch.setattr(native, "preflight", lambda: None)
    native.start()
    assert set(captured["env"]) == {"HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "PATH"}
    assert "s3cret" not in " ".join(map(str, captured["args"])) + json.dumps(captured["env"])
    assert not (tmp_path / "isolated" / ".env").exists()


def test_configure_google_cli_writes_the_env_file_and_reports_bad_input(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "cli.env"
    path.write_text("UNRELATED=kept\n")
    monkeypatch.setattr(
        sys, "argv", ["spending-optimiser", "--configure-google", "--env-file", str(path)]
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False, read=lambda n: CLIENT))
    web.main()
    assert "saved to the .env file" in capsys.readouterr().out
    assert mode(path) == 0o600
    assert dict(dotenv_values(path, interpolate=False)) == {
        "UNRELATED": "kept",
        CLIENT_ID_NAME: "synthetic.apps.googleusercontent.com",
        CLIENT_SECRET_NAME: "s3cret",
    }
    before = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False, read=lambda n: "{"))
    with pytest.raises(SystemExit) as error:
        web.main()
    assert error.value.code == 2 and "s3cret" not in capsys.readouterr().err
    assert path.read_bytes() == before


def test_codex_client_path_is_read_from_env_only_when_no_flag_is_given(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(web.uvicorn, "run", lambda app, **kw: calls.append(app))
    path = tmp_path / "app.env"
    path.write_text(
        "SPENDING_CODEX_CLIENT=/opt/synthetic/codex\nSPENDING_GOOGLE_CLIENT_SECRET=s3cret\n"
    )
    monkeypatch.setenv("SPENDING_OPTIMISER_DB", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(sys, "argv", ["spending-optimiser", "--env-file", str(path)])
    web.main()
    assert str(calls[-1].state.ai.binary) == "/opt/synthetic/codex"
    monkeypatch.setattr(
        sys,
        "argv",
        ["spending-optimiser", "--env-file", str(path), "--codex-client", "/flag/codex"],
    )
    web.main()
    assert str(calls[-1].state.ai.binary) == "/flag/codex"
    for blank in ("", "SPENDING_CODEX_CLIENT=\n"):
        path.write_text(blank)
        monkeypatch.setattr(sys, "argv", ["spending-optimiser", "--env-file", str(path)])
        web.main()
        assert calls[-1].state.ai.binary is None
