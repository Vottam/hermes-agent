from types import SimpleNamespace

from run_agent import AIAgent


def _make_agent(*, model='alias/model', resolved_model=None, context_length=256000, resolved_context_length=None):
    agent = AIAgent.__new__(AIAgent)
    agent.model = model
    agent.provider = 'openrouter'
    agent.base_url = 'https://openrouter.ai/api/v1'
    agent.quiet_mode = True
    agent.context_compressor = SimpleNamespace(context_length=context_length)
    if resolved_model is not None:
        agent._resolved_model = resolved_model
        agent._resolved_context_model = resolved_model
    if resolved_context_length is not None:
        agent._resolved_context_length = resolved_context_length
    return agent


def test_display_helpers_prefer_resolved_fields():
    agent = _make_agent(resolved_model='minimax/minimax-m2.5', resolved_context_length=128000)

    assert agent.get_display_model_name() == 'minimax/minimax-m2.5'
    assert agent.get_display_context_length() == 128000


def test_display_helpers_fall_back_to_configured_model_and_context():
    agent = _make_agent()

    assert agent.get_display_model_name() == 'alias/model'
    assert agent.get_display_context_length() == 256000
