from datetime import datetime
from types import ModuleType, SimpleNamespace
import sys

from gateway.session_cli_turn import _RESULT_PASSTHROUGH
from hermes_cli.gateway_agent import GatewayAgent, _HANDLERS
from tui_gateway.contracts.events import MessageCompletePayload, ModelServedPayload
from tui_gateway.contracts.registry import EVENTS


def _bare_gateway_agent():
    agent = object.__new__(GatewayAgent)
    agent.last_served_model = None
    agent._last_actual_response_model = None
    repaints = []
    agent._print_fn = SimpleNamespace(
        __self__=SimpleNamespace(_invalidate=lambda **kwargs: repaints.append(kwargs)))
    return agent, repaints


def test_executor_event_reaches_gateway_agent_and_repaints_on_change_only():
    agent, repaints = _bare_gateway_agent()

    _HANDLERS["model.served"](agent, {
        "served_model": "gpt-5.6-luna", "requested_model": "codex"}, None, None)

    assert agent.last_served_model == "gpt-5.6-luna"
    assert agent._last_actual_response_model == "gpt-5.6-luna"
    assert len(repaints) == 1

    _HANDLERS["model.served"](agent, {
        "served_model": "gpt-5.6-luna", "requested_model": "codex"}, None, None)
    assert len(repaints) == 1

    GatewayAgent._set_served_model(agent, None)
    assert agent.last_served_model is None
    assert agent._last_actual_response_model is None
    assert len(repaints) == 2


def test_model_identity_contract_and_final_result_passthrough():
    assert EVENTS["model.served"].payload is ModelServedPayload
    event = ModelServedPayload(
        served_model="gpt-5.6-luna", requested_model="codex")
    complete = MessageCompletePayload(
        text="ok", requested_model="codex", served_model=event.served_model)

    assert event.model_dump() == {
        "served_model": "gpt-5.6-luna", "requested_model": "codex"}
    assert complete.model_dump()["served_model"] == "gpt-5.6-luna"
    assert {"requested_model", "served_model"} <= set(_RESULT_PASSTHROUGH)


def test_status_bar_snapshot_prefers_the_gateway_served_model():
    from hermes_cli.cli_status_bar_mixin import CLIStatusBarMixin

    fake_cli = ModuleType("cli")
    setattr(fake_cli, "_reverse_alias_for_display", lambda value: "alias")
    setattr(fake_cli, "datetime", datetime)
    setattr(fake_cli, "format_duration_compact", lambda seconds: str(int(seconds)))
    old_cli = sys.modules.get("cli")
    sys.modules["cli"] = fake_cli
    try:
        class Harness(CLIStatusBarMixin):
            def _get_status_bar_session_title(self):
                return ""

            def _get_status_bar_field_set(self):
                return frozenset()

        view = Harness()
        setattr(view, "agent", SimpleNamespace(
            model="codex", last_served_model="gpt-5.6-luna",
            _last_actual_response_model="gpt-5.6-luna"))
        setattr(view, "model", "codex")
        setattr(view, "session_start", datetime.now())
        setattr(view, "_prompt_start_time", None)
        setattr(view, "_prompt_duration", 0.0)
        setattr(view, "_last_turn_finished_at", None)
        setattr(view, "_battery_visible", False)
        setattr(view, "_background_tasks", {})
        setattr(view, "_focus_view_enabled", False)

        snapshot = view._get_status_bar_snapshot()
        assert snapshot["model_name"] == "gpt-5.6-luna"
        assert snapshot["model_short"] == "gpt-5.6-luna"
    finally:
        if old_cli is None:
            sys.modules.pop("cli", None)
        else:
            sys.modules["cli"] = old_cli
