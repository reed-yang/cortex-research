"""Per-turn research prompts preserve native behavior and caller options."""

from types import SimpleNamespace

import pytest

from cortex_platform.runtime.hermes import HermesRunInput, _NativeHermesBackend, hermes_turn_prompt
from .test_native_seam import MemorySessionDB


def request(**overrides):
    values = dict(run_id="run", attempt_id="attempt", session_ref="session",
                  user_message="question", system_message="current directive",
                  conversation_history=(), execution_token=object(),
                  metadata={"cortex_research_prompt_mode": "ephemeral_v1"})
    values.update(overrides)
    return HermesRunInput(**values)


@pytest.mark.parametrize("configured", [None, "", "configured policy"])
def test_options_are_preserved_without_accumulating_old_packets(configured):
    options = {"model": "test", "ephemeral_system_prompt": configured}
    persistent, first = hermes_turn_prompt(request(), options)
    assert persistent is None
    assert first["ephemeral_system_prompt"] == (
        "configured policy\n\ncurrent directive" if configured else "current directive")
    _, second = hermes_turn_prompt(request(system_message="changed packet"), options)
    assert "current directive" not in second["ephemeral_system_prompt"]
    assert options == {"model": "test", "ephemeral_system_prompt": configured}
    generic, unchanged = hermes_turn_prompt(request(metadata={}), options)
    assert generic == "current directive"
    assert unchanged == options and unchanged is not options


@pytest.mark.parametrize("prompt", [None, "", "  "])
def test_opt_in_cannot_silently_drop_the_current_directive(prompt):
    with pytest.raises(ValueError, match="missing"):
        hermes_turn_prompt(request(system_message=prompt), {})


def test_native_agent_receives_ephemeral_prompt_without_persistent_packet():
    captured = {}

    class Agent:
        def __init__(self, **options):
            captured["options"] = options
            self.session_id = options["session_id"]

        def run_conversation(self, user_message, **kwargs):
            captured["conversation"] = kwargs
            return {"final_response": "answer"}

    backend = _NativeHermesBackend(
        hermes_state=SimpleNamespace(SessionDB=MemorySessionDB), agent_class=Agent,
        set_approval_callback=lambda _: None, session_db_path=None,
        agent_options={"ephemeral_system_prompt": "configured policy"})
    session = backend.open_session({})
    token = backend.reserve_attempt("run", "attempt")
    result = backend.run(request(session_ref=session.session_ref, execution_token=token), lambda _: None)
    assert result.final_response == "answer"
    assert result.session_ref == session.session_ref
    assert captured["options"]["ephemeral_system_prompt"] == "configured policy\n\ncurrent directive"
    assert captured["conversation"]["system_message"] is None
