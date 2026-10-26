"""Run the real sign-in window script against a recording browser stand-in (needs Node)."""

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "fixtures" / "provider_connect_harness.js"


@pytest.fixture(scope="module")
def behavior():
    if NODE is None:
        pytest.skip("Node is not installed")
    script = files("spending_optimiser").joinpath("static/provider-connect.js")
    done = subprocess.run(
        [NODE, str(HARNESS), str(script)], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_clicking_connect_opens_one_named_window_and_waits_for_the_server(behavior):
    result = behavior["opens"]
    assert result["error"] is None
    assert result["opened"] == [["", "provider-auth", "popup,width=520,height=720"]]
    assert result["prevented"] == 0  # the form still submits into that window
    assert result["waitingShown"] and not result["blockedShown"]
    # Only the app's own status endpoint is polled, with cookies and without caching.
    assert {tuple(call) for call in result["fetches"]} == {
        ("/gmail/status", "same-origin", "no-store")
    }
    # An idle answer for the old attempt is ignored; the finished new attempt reloads once.
    assert result["reloadsAfterEachPoll"] == [0, 1]
    assert result["reloads"] == 1 and result["popupClosedBeforeReload"] == 1


def test_a_blocked_window_explains_itself_and_continues_only_when_asked(behavior):
    result = behavior["blocked"]
    assert result["error"] is None
    assert result["before"] == {"prevented": 1, "focused": 1, "fetches": 0}
    assert result["blockedShown"] and not result["waitingShown"]
    assert result["removed"] == ["target"]
    assert result["submitted"] == {"target": "", "display": "window"}
    assert result["timers"] == 0  # no polling for a window that never opened


def test_same_window_forms_and_failing_polls_are_harmless(behavior):
    assert behavior["sameWindow"] == {"error": None, "opened": 0, "prevented": 0}
    assert behavior["failing"] == {"error": None, "reloads": 0, "stopped": True}


def test_a_page_reloaded_mid_sign_in_resumes_and_the_result_window_closes(behavior):
    assert behavior["resume"] == {
        "error": None,
        "fetches": ["/ai/status", "/ai/status"],
        "reloads": 1,
    }
    result = behavior["result"]
    assert result["error"] is None and result["closeShown"]
    assert result["manual"] == 1 and result["automatic"] == 1 and result["delay"] == 1800
