"""Portable regression of the pinned native reply, not a permissive fake startup."""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from spending_optimiser.codex_client import CONFIG, AIProblem, CodexClient

RECORDED_REPLY = json.loads(
    (Path(__file__).parent / "fixtures/codex-0.164.0-chatgpt-requirements.json").read_text()
)


@pytest.fixture(autouse=True)
def no_native_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Portable contract tests must not start a native client")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize(
    "reply",
    [
        RECORDED_REPLY,
        {"requirements": None},
        {"requirements": {"allowedLoginMethods": ["chatgpt"]}},
    ],
    ids=["recorded-native-production-reply", "explicit-null", "only-own-login-pin"],
)
def test_accepts_only_absence_of_policy_or_own_chatgpt_pin(tmp_path, reply):
    native = CodexClient("/fake", tmp_path)
    calls = []

    def request(method, params=None):
        calls.append(method)
        return reply

    native.request = request
    try:
        native.check_requirements()
        assert calls == ["configRequirements/read"]
    finally:
        native.close()


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"requirements": {}},
        {"requirements": {"allowedLoginMethods": None, "network": None}},
        {"requirements": dict.fromkeys(RECORDED_REPLY["requirements"])},
        *({"requirements": wrong} for wrong in [False, True, 0, 1, "", "chatgpt", [], ["chatgpt"]]),
        *(
            {"requirements": RECORDED_REPLY["requirements"] | {"allowedLoginMethods": wrong}}
            for wrong in [
                None,
                [],
                ["api"],
                ["chatgpt", "api"],
                ["chatgpt", "chatgpt"],
                "chatgpt",
                {"chatgpt": True},
                True,
            ]
        ),
        *(
            {"requirements": RECORDED_REPLY["requirements"] | extra}
            for extra in [
                {"featureRequirements": {"shell_tool": True}},
                {"network": {"enabled": True}},
                {"modelProvider": "another-provider"},
                {"hooks": {}},
                {"application": False},
                {"unknownFutureRequirement": 0},
                {"unknownFutureRequirement": []},
                {"unknownFutureRequirement": ""},
            ]
        ),
    ],
)
def test_missing_malformed_or_additional_policy_stops_before_config(tmp_path, reply):
    native = CodexClient("/fake", tmp_path)
    calls = []

    def request(method, params=None):
        calls.append(method)
        return reply

    native.request = request
    try:
        with pytest.raises(AIProblem) as failure:
            native.preflight()
        assert failure.value.code == "unsafe_configuration"
        assert calls == ["configRequirements/read"]
    finally:
        native.close()


@pytest.mark.parametrize("unsafe", ["effective-config", "skill-errors", "enabled-skill"])
def test_own_login_pin_does_not_bypass_configuration_or_skill_gates(tmp_path, unsafe):
    native = CodexClient("/fake", tmp_path)
    cfg = tomllib.loads(CONFIG) | {"chatgpt_base_url": "https://chatgpt.com/backend-api/"}
    if unsafe == "effective-config":
        cfg["permissions"]["noio"]["network"]["enabled"] = True
    calls = []

    def request(method, params=None):
        calls.append(method)
        if method == "configRequirements/read":
            return RECORDED_REPLY
        if method == "config/read":
            return {"config": cfg}
        if method == "skills/list":
            return {
                "data": [
                    {
                        "errors": ["synthetic error"] if unsafe == "skill-errors" else [],
                        "skills": [{"path": "/synthetic/SKILL.md", "enabled": True}],
                    }
                ]
            }
        if method == "skills/config/write":
            return {}
        raise AssertionError("No model operation may follow unsafe preflight")

    native.request = request
    try:
        with pytest.raises(AIProblem, match="privacy boundary"):
            native.analyse("synthetic public input", {"type": "object"})
        assert calls[0] == "configRequirements/read"
        assert "thread/start" not in calls
        if unsafe == "enabled-skill":
            assert calls[-2:] == ["skills/config/write", "skills/list"]
    finally:
        native.close()


@pytest.mark.parametrize("late_policy", [False, True])
def test_analysis_rechecks_recorded_pin_before_and_after_thread(tmp_path, late_policy):
    native = CodexClient("/fake", tmp_path)
    cfg = tomllib.loads(CONFIG) | {"chatgpt_base_url": "https://chatgpt.com/backend-api/"}
    calls, policy_reads = [], []

    def request(method, params=None):
        calls.append(method)
        if method == "configRequirements/read":
            policy_reads.append(method)
            if late_policy and len(policy_reads) == 2:
                return {"requirements": RECORDED_REPLY["requirements"] | {"network": {}}}
            return RECORDED_REPLY
        if method == "config/read":
            return {"config": cfg}
        if method == "skills/list":
            return {"data": [{"skills": [], "errors": []}]}
        if method == "thread/start":
            return {
                "thread": {"id": "synthetic"},
                "modelProvider": "openai",
                "sandbox": {"networkAccess": False},
                "activePermissionProfile": {"id": "noio"},
                "approvalPolicy": "never",
                "instructionSources": [],
            }
        return {}

    native.request = request
    native.read_answer = lambda: "{}"
    try:
        if late_policy:
            with pytest.raises(AIProblem, match="privacy boundary"):
                native.analyse("synthetic public input", {"type": "object"})
            assert "turn/start" not in calls
        else:
            assert native.analyse("synthetic public input", {"type": "object"}) == "{}"
            assert calls[-2:] == ["turn/start", "thread/unsubscribe"]
        assert calls[:5] == [
            "configRequirements/read",
            "config/read",
            "skills/list",
            "skills/list",
            "thread/start",
        ]
        assert len(policy_reads) == 2
    finally:
        native.close()
