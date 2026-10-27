"""Opt-in pinned-native pre-auth contract; every native child is seatbelt-confined.

No real auth/account/model method is allowed through the test transport. Web tests
use native initialize/config/skills and schema-valid synthetic auth in-process.
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from spending_optimiser.codex_client import ARM64_SHA256, CONFIG, CodexClient

PREAUTH = {
    "initialize",
    "initialized",
    "configRequirements/read",
    "config/read",
    "skills/list",
    "skills/config/write",
}
FIXTURE = Path(__file__).parent / "fixtures/codex-0.164.0-chatgpt-requirements.json"


@pytest.fixture(scope="module")
def native_contract(tmp_path_factory):
    configured = os.environ.get("NATIVE_CONTRACT_BINARY")
    if not configured:
        pytest.skip("opt-in: set NATIVE_CONTRACT_BINARY to the pinned official binary")
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    binary = Path(configured)
    assert binary.is_absolute() and binary.is_file()
    with binary.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == ARM64_SHA256
    assert Path("/usr/bin/sandbox-exec").is_file(), "no unsandboxed fallback"
    root = tmp_path_factory.mktemp("native-contract")
    for folder in ("home", "codex", "cwd", "schema"):
        (root / folder).mkdir()
    # Same network/Keychain denials as the retained investigation. HOME is
    # disposable, but the real owner's Keychain path must still be denied.
    keychains = json.dumps(str(Path.home() / "Library/Keychains"))
    profile = root / "deny.sb"
    profile.write_text(
        "(version 1)\n(allow default)\n(deny network*)\n"
        "(deny mach-lookup\n"
        ' (global-name "com.apple.SecurityServer")\n'
        ' (global-name "com.apple.securityd")\n'
        ' (global-name "com.apple.secd")\n'
        ' (global-name "com.apple.security.agent")\n'
        ' (global-name "com.apple.security.keychain-cache")\n'
        ' (global-name-prefix "com.apple.security."))\n'
        f"(deny file-read* file-write* (subpath {keychains}))\n"
    )
    env = {
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "codex"),
        "XDG_CONFIG_HOME": str(root / "home/.config"),
        "PATH": "/usr/bin:/bin",
    }
    confinement = ["/usr/bin/sandbox-exec", "-f", str(profile)]
    # This command only emits public protocol schema; it does not initialize auth.
    subprocess.run(
        [
            *confinement,
            str(binary),
            "app-server",
            "generate-json-schema",
            "--out",
            str(root / "schema"),
        ],
        cwd=root / "cwd",
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return binary, root, env, confinement


@pytest.mark.parametrize("scenario", ["preflight", "connect", "restore", "login-failure"])
def test_real_native_production_boundary(native_contract, scenario):
    binary, root, env, confinement = native_contract
    scratch = root / scenario
    scratch.mkdir()
    result = subprocess.run(
        [
            *confinement,
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--probe",
            str(binary),
            str(scratch),
            str(root / "schema"),
            scenario,
        ],
        cwd=root / "cwd",
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())  # Public method-only evidence, visible with pytest -s.
    evidence = json.loads(result.stdout)
    assert evidence["scenario"] == scenario
    assert set(evidence["native_methods"]) <= PREAUTH
    assert "configRequirements/read" in evidence["native_methods"]
    assert evidence["production_reply_matches_recording"] is True
    assert evidence["auth_is_synthetic"] is True


class PreauthOnlyClient(CodexClient):
    """A second, send-level deny gate even if request mediation regresses."""

    def __init__(self, binary, home):
        super().__init__(binary, home)
        self.native_methods = []
        self.requirements_replies = []

    def send(self, value):
        method = value.get("method")
        assert method in PREAUTH, f"Native method forbidden by test: {method}"
        if method == "initialize":
            assert (self.home / "config.toml").read_text() == CONFIG
        if method == "skills/config/write":
            assert Path(value["params"]["path"]).is_relative_to(self.home)
        self.native_methods.append(method)
        super().send(value)

    def request(self, method, params=None, timeout=20):
        assert method in PREAUTH, f"Native request forbidden by test: {method}"
        reply = super().request(method, params, timeout)
        if method == "configRequirements/read":
            self.requirements_replies.append(reply)
        return reply


def schema_matches(value, schema, definitions):
    """Strict subset needed by the generated auth schemas; reject unknown validators."""
    annotations = {"$schema", "title", "description", "default"}
    supported = {"$ref", "oneOf", "anyOf", "type", "enum", "properties", "required"}
    assert not set(schema) - annotations - supported, "unsupported schema keyword"
    if "$ref" in schema:
        return schema_matches(value, definitions[schema["$ref"].rsplit("/", 1)[1]], definitions)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            matches = sum(schema_matches(value, item, definitions) for item in schema[keyword])
            if (keyword == "oneOf" and matches != 1) or not matches:
                return False
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        actual = {
            dict: "object",
            list: "array",
            str: "string",
            bool: "boolean",
            type(None): "null",
            int: "integer",
            float: "number",
        }[type(value)]
        if actual not in types:
            return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        if not set(schema.get("required", [])) <= value.keys():
            return False
        for key, child in schema.get("properties", {}).items():
            if key in value and not schema_matches(value[key], child, definitions):
                return False
    return True


def probe(binary, scratch, schema_dir, scenario):
    from fastapi.testclient import TestClient

    from spending_optimiser.web import create_app

    definitions = json.loads(
        (schema_dir / "codex_app_server_protocol.v2.schemas.json").read_text()
    )["definitions"]

    def validate(name, value):
        assert schema_matches(value, definitions[name], definitions), name

    # Negative controls for the schema validator used at the fake-auth seam.
    assert not schema_matches({"type": "chatgpt"}, definitions["LoginAccountResponse"], definitions)
    assert not schema_matches(
        {"success": "false"}, definitions["AccountLoginCompletedNotification"], definitions
    )
    account = {"type": "chatgpt", "email": "synthetic@example.invalid", "planType": "plus"}

    class FakeAuthClient(PreauthOnlyClient):
        account_value = account if scenario == "restore" else None

        def request(self, method, params=None, timeout=20):
            if method == "account/read":
                validate("GetAccountParams", params)
                assert params == {"refreshToken": False}
                reply = {"account": self.account_value, "requiresOpenaiAuth": True}
                validate("GetAccountResponse", reply)
                return reply
            if method == "account/login/start":
                validate("LoginAccountParams", params)
                assert params == {"type": "chatgpt"}
                reply = {
                    "type": "chatgpt",
                    "loginId": "synthetic-login",
                    "authUrl": "https://auth.openai.com/oauth/authorize?synthetic=1",
                }
                validate("LoginAccountResponse", reply)
                return reply
            return super().request(method, params, timeout)

        def complete(self, success):
            notification = {
                "loginId": "synthetic-login",
                "success": success,
                "error": None if success else "synthetic denial",
            }
            validate("AccountLoginCompletedNotification", notification)
            self.pending.append({"method": "account/login/completed", "params": notification})
            if success:
                self.account_value = account
                notification = {"authMode": "chatgpt", "planType": "plus"}
                validate("AccountUpdatedNotification", notification)
                self.pending.append({"method": "account/updated", "params": notification})

    if scenario == "preflight":
        native = PreauthOnlyClient(binary, scratch / "client")
        try:
            native.start()
            native.preflight()  # same analysis-time gate, no thread/model operation
        finally:
            native.close()
    else:
        app = create_app(scratch / "ledger.sqlite3", codex_client=binary, env_file=scratch / ".env")
        app.state.ai.client_factory = FakeAuthClient
        with TestClient(app, base_url="http://127.0.0.1:8894") as web:
            web.get("/ai")
            response = web.post(
                "/ai/connect", data={"csrf": web.cookies["ledger_csrf"], "display": "popup"}
            )
            assert response.status_code == 200
            native = app.state.ai.client
            assert native is not None, app.state.ai.problem
            if scenario == "restore":
                assert app.state.ai.connection == {"email": account["email"], "plan": "plus"}
                assert app.state.ai.authorization_url is None
            else:
                assert "Continue to ChatGPT" in response.text
                assert web.get("/ai/status").json()["state"] == "pending"
                native.complete(scenario == "connect")
                assert web.get("/ai/status").json()["state"] == "idle"
            page = web.get("/ai").text
            if scenario == "login-failure":
                assert "ChatGPT sign-in did not complete. Connect again." in page
                assert app.state.ai.connection is None
                assert native.proc is None
            else:
                assert "ChatGPT connected" in page and account["email"] in page
                native.preflight()

    # Behavioral deny controls: neither auth nor model can ever reach native send.
    for forbidden in (
        "account/read",
        "account/login/start",
        "account/logout",
        "thread/start",
        "turn/start",
    ):
        with pytest.raises(AssertionError, match="Native method forbidden"):
            native.send({"method": forbidden, "params": {}})
    recorded = json.loads(FIXTURE.read_text())
    assert native.requirements_replies and all(r == recorded for r in native.requirements_replies)
    print(
        json.dumps(
            {
                "scenario": scenario,
                "native_methods": native.native_methods,
                "production_reply_matches_recording": True,
                "auth_is_synthetic": True,
            }
        )
    )


if __name__ == "__main__":
    assert len(sys.argv) == 6 and sys.argv[1] == "--probe"
    probe(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
