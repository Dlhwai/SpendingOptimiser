"""Restricted official client, native ChatGPT authentication; no custom OAuth/token broker."""

import hashlib
import json
import os
import platform
import selectors
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .ai_inputs import INSTRUCTIONS

VERSION = "0.164.0"
ARM64_SHA256 = "d8990e3ee7acfbc26f91265b12a1a83f0fee40d0e0f8f1f40a37d4ec21f499cf"
DISABLED = (
    "apps",
    "plugins",
    "remote_plugin",
    "plugin_sharing",
    "multi_agent",
    "multi_agent_v2",
    "hooks",
    "memories",
    "shell_tool",
    "shell_snapshot",
    "skill_mcp_dependency_install",
    "skill_search",
    "workspace_dependencies",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "in_app_browser",
    "image_generation",
    "view_image",
    "request_permissions_tool",
    "tool_suggest",
    "code_mode",
    "code_mode_host",
    "code_mode_prewarm",
    "goals",
    "secret_auth_storage",
)
CONFIG = (
    """model = "gpt-6.1-sol"
model_provider = "openai"
forced_login_method = "chatgpt"
cli_auth_credentials_store = "keyring"
approval_policy = "never"
approvals_reviewer = "user"
default_permissions = "noio"
web_search = "disabled"
project_doc_max_bytes = 0
include_environment_context = false
include_environment_context_time = false
include_permissions_instructions = false
include_collaboration_mode_instructions = false
include_apps_instructions = false
check_for_update_on_startup = false
[history]
persistence = "none"
[analytics]
enabled = false
[feedback]
enabled = false
[otel]
exporter = "none"
metrics_exporter = "none"
trace_exporter = "none"
log_user_prompt = false
log_agent_responses = false
[permissions.noio.filesystem]
":root" = "deny"
[permissions.noio.network]
enabled = false
[features]
"""
    + "\n".join(f"{name} = false" for name in DISABLED)
    + "\n"
)

MESSAGES = {
    "not_configured": (
        "Codex is not configured. Install the supported official client locally and supply "
        "--codex-client. No API key is accepted."
    ),
    "unsupported_client": (
        "Requires the unmodified official Codex 0.164.0 macOS Apple Silicon binary. "
        "Other clients/platforms are unavailable."
    ),
    "unsafe_configuration": (
        "Client configuration cannot establish the privacy boundary. No proposal was installed. "
        "Check this app's isolated client setup; do not weaken system policy."
    ),
    "connection_failed": (
        "Official client connection failed. Unlock native Keychain and reconnect. "
        "No plaintext fallback is permitted."
    ),
    "timeout": (
        "The official client timed out and was closed. Reconnect before retrying; "
        "a submitted analysis may have consumed subscription allowance."
    ),
    "not_connected": "Connect an eligible ChatGPT session before requesting analysis.",
    "account_changed": (
        "The provider account changed. Reconnect explicitly; "
        "the previous proposal cannot be accepted under another account."
    ),
    "subscription_required": (
        "No eligible saved ChatGPT subscription session was found. "
        "API-key authentication and separately billed API fallback are refused."
    ),
    "analysis_failed": (
        "Analysis failed or access/limits were rejected. No proposal was installed. "
        "Check subscription/model availability and reconnect to retry."
    ),
    "invalid_output": (
        "Model output was invalid, unsupported or not backed by saved evidence. "
        "No proposal was saved or installed."
    ),
    "consent_required": (
        "Preview the saved input and explicitly confirm this analysis. Nothing was sent."
    ),
}


class AIProblem(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def configuration_safe(cfg):
    """Reject inherited authority rather than assuming empty tables erase it."""
    expected = {
        "model": "gpt-6.1-sol",
        "model_provider": "openai",
        "forced_login_method": "chatgpt",
        "cli_auth_credentials_store": "keyring",
        "approval_policy": "never",
        "approvals_reviewer": "user",
        "default_permissions": "noio",
        "web_search": "disabled",
        "include_environment_context": False,
        "include_environment_context_time": False,
        "include_permissions_instructions": False,
        "include_collaboration_mode_instructions": False,
        "include_apps_instructions": False,
        "project_doc_max_bytes": 0,
    }
    if any(cfg.get(k) != v for k, v in expected.items()):
        return False
    if any(cfg.get("features", {}).get(k) is not False for k in DISABLED):
        return False
    forbidden = (
        "mcp_servers",
        "apps",
        "plugins",
        "marketplaces",
        "hooks",
        "notify",
        "agents",
        "sandbox_mode",
        "sandbox_workspace_write",
        "model_providers",
        "openai_base_url",
        "model_catalog_json",
        "model_instructions_file",
        "instructions",
        "developer_instructions",
        "compact_prompt",
        "experimental_compact_prompt_file",
        "experimental_thread_store_endpoint",
        "experimental_realtime_ws_base_url",
        "experimental_thread_store",
        "orchestrator",
    )
    if any(cfg.get(k) for k in forbidden):
        return False
    if cfg.get("chatgpt_base_url") != "https://chatgpt.com/backend-api/":
        return False
    profile = cfg.get("permissions", {}).get("noio", {})
    filesystem = {k: v for k, v in profile.get("filesystem", {}).items() if v is not None}
    network = {k: v for k, v in profile.get("network", {}).items() if v is not None}
    return (
        filesystem == {":root": "deny"}
        and network == {"enabled": False}
        and not profile.get("extends")
        and not profile.get("workspace_roots")
    )


class CodexClient:
    """Only invoked by explicit POST actions. Version/hash pinned to native enforcement proof."""

    def __init__(self, binary, home):
        self.binary, self.home = Path(binary), Path(home)
        self.proc = None
        self.selector = selectors.DefaultSelector()
        self.buffer = b""
        self.pending = []
        self.counter = 0

    def start(self):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise AIProblem("unsupported_client")
        if not self.binary.is_absolute() or not self.binary.is_file():
            raise AIProblem("unsupported_client")
        with self.binary.open("rb") as binary:
            checksum = hashlib.file_digest(binary, "sha256").hexdigest()
        if checksum != ARM64_SHA256:
            raise AIProblem("unsupported_client")
        if self.home.is_symlink():
            raise AIProblem("unsafe_configuration")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        for name in ("empty", "runtime-home"):
            folder = self.home / name
            if folder.is_symlink():
                raise AIProblem("unsafe_configuration")
            folder.mkdir(exist_ok=True, mode=0o700)
        config = self.home / "config.toml"
        if config.is_symlink():
            raise AIProblem("unsafe_configuration")
        # Only public configuration is written. Tokens belong exclusively to the
        # official client's mandatory native keyring, keyed by this CODEX_HOME.
        with config.open("w") as stream:
            os.chmod(config, 0o600)
            stream.write(CONFIG)
        env = {
            "HOME": str(self.home / "runtime-home"),
            "CODEX_HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "runtime-home" / ".config"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }
        self.proc = subprocess.Popen(
            [str(self.binary), "app-server", "--strict-config", "--stdio"],
            cwd=self.home / "empty",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "spending_optimiser", "version": "1"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self.send({"method": "initialized", "params": {}})
        self.preflight()

    def send(self, value):
        try:
            self.proc.stdin.write((json.dumps(value) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, AttributeError):
            raise AIProblem("connection_failed") from None

    def event(self, deadline):
        while time.monotonic() < deadline:
            if self.pending:
                event = self.pending.pop(0)
                if "method" in event and "id" in event:
                    # Never grant approvals, tools, external tokens or host callbacks.
                    self.close()
                    raise AIProblem("unsafe_configuration")
                return event
            if not self.selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                if self.proc.poll() is not None:
                    raise AIProblem("connection_failed")
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise AIProblem("connection_failed")
            self.buffer += chunk
            if len(self.buffer) > 2_000_000:
                raise AIProblem("connection_failed")
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError()
                    self.pending.append(value)
                except (ValueError, UnicodeError):
                    raise AIProblem("connection_failed") from None
        self.close()
        raise AIProblem("timeout")

    def request(self, method, params=None, timeout=20):
        self.counter += 1
        identity = self.counter
        self.send({"id": identity, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        deferred = []
        while True:
            event = self.event(deadline)
            if event.get("id") == identity:
                self.pending = deferred + self.pending
                if "error" in event or not isinstance(event.get("result"), dict):
                    raise AIProblem("connection_failed")
                return event["result"]
            deferred.append(event)
            if len(deferred) > 1024:
                raise AIProblem("connection_failed")

    def check_requirements(self):
        # Managed/MDM/cloud requirements can pin values beyond the user config.
        # This milestone qualifies unmanaged clients only, never overrides policy.
        result = self.request("configRequirements/read")
        if "requirements" not in result or result["requirements"] is not None:
            raise AIProblem("unsafe_configuration")

    def preflight(self):
        self.check_requirements()
        config = self.request("config/read", {"includeLayers": True}).get("config", {})
        if not configuration_safe(config):
            raise AIProblem("unsafe_configuration")
        cwd = str(self.home / "empty")
        listing = self.request("skills/list", {"cwds": [cwd], "forceReload": True})
        for row in listing.get("data", []):
            if row.get("errors"):
                raise AIProblem("unsafe_configuration")
            for skill in row.get("skills", []):
                if skill.get("enabled"):
                    self.request("skills/config/write", {"path": skill["path"], "enabled": False})
        checked = self.request("skills/list", {"cwds": [cwd], "forceReload": True})
        if not checked.get("data") or any(
            row.get("errors") or any(s.get("enabled") for s in row.get("skills", []))
            for row in checked["data"]
        ):
            raise AIProblem("unsafe_configuration")

    def account(self):
        account = self.request("account/read", {"refreshToken": False}).get("account")
        if account is None:
            return None
        if (
            not isinstance(account, dict)
            or account.get("type") != "chatgpt"
            or account.get("planType")
            not in {
                "plus",
                "pro",
                "business",
                "team",
                "enterprise",
                "edu",
                "go",
            }
            or not isinstance(account.get("email"), str)
        ):
            raise AIProblem("subscription_required")
        return {"email": account["email"], "plan": account["planType"]}

    def login(self):
        result = self.request("account/login/start", {"type": "chatgpt"})
        url = result.get("authUrl", "")
        try:
            parsed = urlsplit(url)
            if (
                not isinstance(url, str)
                or len(url) > 8192
                or parsed.scheme != "https"
                or parsed.hostname != "auth.openai.com"
                or parsed.username
                or parsed.port not in {None, 443}
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise AIProblem("connection_failed") from None
        return url

    def analyse(self, text, schema):
        self.preflight()
        thread = self.request(
            "thread/start",
            {
                "cwd": str(self.home / "empty"),
                "ephemeral": True,
                "approvalPolicy": "never",
                "baseInstructions": INSTRUCTIONS,
                "developerInstructions": "",
            },
        )
        if (
            thread.get("activePermissionProfile", {}).get("id") != "noio"
            or thread.get("modelProvider") != "openai"
            or thread.get("sandbox", {}).get("networkAccess") is not False
            or thread.get("approvalPolicy") != "never"
            or thread.get("instructionSources")
        ):
            raise AIProblem("unsafe_configuration")
        try:
            # Thread startup can refresh cloud requirements after authentication.
            self.check_requirements()
            self.request(
                "turn/start",
                {
                    "threadId": thread["thread"]["id"],
                    "input": [{"type": "text", "text": text}],
                    "outputSchema": schema,
                },
            )
            answer = self.read_answer()
            self.request("thread/unsubscribe", {"threadId": thread["thread"]["id"]})
            return answer
        except AIProblem:
            self.close()
            raise

    def read_answer(self):
        deadline, answers = time.monotonic() + 120, []
        while True:
            event = self.event(deadline)
            if event.get("method") == "item/completed":
                item = event.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") in {
                    None,
                    "final_answer",
                }:
                    answers.append(item.get("text", ""))
            if event.get("method") == "turn/completed":
                if (
                    event.get("params", {}).get("turn", {}).get("status") != "completed"
                    or len(answers) != 1
                ):
                    raise AIProblem("analysis_failed")
                return answers[0]

    def logout(self):
        self.request("account/logout")

    def close(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
            for stream in (self.proc.stdin, self.proc.stdout):
                stream.close()
            self.proc = None
        self.selector.close()
