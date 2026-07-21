"""Tests for status bar resolved model/context invalidation."""

from types import SimpleNamespace
from unittest.mock import patch


def _make_agent(**overrides):
    """Create a minimal agent mock for status bar tests."""
    agent = SimpleNamespace(
        model=overrides.get('model', '@preset/hermes'),
        provider=overrides.get('provider', 'openrouter'),
        base_url=overrides.get('base_url', 'https://openrouter.ai/api/v1'),
        api_key=overrides.get('api_key', 'test-key-xyz'),
        _custom_providers=overrides.get('_custom_providers', None),
        _resolved_provider=overrides.get('_resolved_provider', overrides.get('provider', 'openrouter')),
        _resolved_model=overrides.get('_resolved_model', None),
        _resolved_context_length=overrides.get('_resolved_context_length', None),
        last_prompt_tokens=overrides.get('last_prompt_tokens', 0),
        compression_count=overrides.get('compression_count', 0),
        context_compressor=overrides.get(
            'context_compressor',
            SimpleNamespace(
                context_length=overrides.get('context_length', 200000),
                update_model=lambda *a, **k: True,
            ),
        ),
    )
    return agent


def _call(agent, model_name):
    """Call the real conversation_loop function with a mock response."""
    response = SimpleNamespace(model=model_name)
    from agent.conversation_loop import _update_resolved_model_metadata
    _update_resolved_model_metadata(agent, response)


# --- Model switching tests ---

def test_model_change_recalculates_context():
    agent = _make_agent(_resolved_model='model-a', _resolved_context_length=256000)
    with patch('agent.model_metadata.get_model_context_length', return_value=1000000):
        _call(agent, 'model-b')
    assert agent._resolved_context_length == 1000000
    assert agent._resolved_model == 'model-b'


def test_five_successive_changes_all_recalculate():
    agent = _make_agent()
    for i, ctx in enumerate([256000, 1000000, 32000, 8000, 512000], 1):
        with patch('agent.model_metadata.get_model_context_length', return_value=ctx):
            _call(agent, f'model-{i}')
        assert agent._resolved_context_length == ctx


def test_same_model_does_not_re_resolve():
    agent = _make_agent(_resolved_model='model-a', _resolved_context_length=256000)
    with patch('agent.model_metadata.get_model_context_length') as m:
        _call(agent, 'model-a')
    m.assert_not_called()
    assert agent._resolved_context_length == 256000


def test_failed_resolution_invalidates_old_context():
    agent = _make_agent(_resolved_model='model-a', _resolved_context_length=256000)
    with patch('agent.model_metadata.get_model_context_length', side_effect=Exception('fail')):
        _call(agent, 'model-b')
    assert agent._resolved_context_length is None


def test_response_without_model_preserves_state():
    agent = _make_agent(_resolved_model='model-a', _resolved_context_length=256000)
    from agent.conversation_loop import _update_resolved_model_metadata
    _update_resolved_model_metadata(agent, SimpleNamespace())
    assert agent._resolved_model == 'model-a'
    assert agent._resolved_context_length == 256000


def test_response_with_empty_model_preserves_state():
    agent = _make_agent(_resolved_model='model-a', _resolved_context_length=256000)
    from agent.conversation_loop import _update_resolved_model_metadata
    _update_resolved_model_metadata(agent, SimpleNamespace(model=''))
    assert agent._resolved_model == 'model-a'


def test_agent_model_not_overwritten():
    agent = _make_agent(model='@preset/hermes')
    with patch('agent.model_metadata.get_model_context_length', return_value=1000000):
        _call(agent, 'model-b')
    assert agent.model == '@preset/hermes'


def test_last_prompt_tokens_unchanged():
    agent = _make_agent(last_prompt_tokens=1234)
    with patch('agent.model_metadata.get_model_context_length', return_value=256000):
        _call(agent, 'model-a')
    assert agent.last_prompt_tokens == 1234


def test_compression_count_unchanged():
    agent = _make_agent(compression_count=5)
    with patch('agent.model_metadata.get_model_context_length', return_value=256000):
        _call(agent, 'model-a')
    assert agent.compression_count == 5


def test_compressor_update_model_not_called():
    mock = lambda *a, **k: True
    agent = _make_agent()
    agent.context_compressor.update_model = mock
    with patch('agent.model_metadata.get_model_context_length', return_value=256000):
        _call(agent, 'model-a')


def test_custom_providers_passed_through():
    custom = [SimpleNamespace(name='my-provider')]
    agent = _make_agent(_custom_providers=custom)
    with patch('agent.model_metadata.get_model_context_length', return_value=256000) as m:
        _call(agent, 'model-a')
    assert m.call_args[1].get('custom_providers') is custom


def test_api_key_passed_through():
    agent = _make_agent(api_key='test-key-xyz')
    with patch('agent.model_metadata.get_model_context_length', return_value=256000) as m:
        _call(agent, 'model-a')
    assert m.call_args[1].get('api_key') == 'test-key-xyz'


def test_provider_passed_through():
    agent = _make_agent(provider='openrouter')
    with patch('agent.model_metadata.get_model_context_length', return_value=256000) as m:
        _call(agent, 'model-a')
    assert m.call_args[1].get('provider') == 'openrouter'


def test_base_url_passed_through():
    agent = _make_agent(base_url='https://example.com/v1')
    with patch('agent.model_metadata.get_model_context_length', return_value=256000) as m:
        _call(agent, 'model-a')
    assert m.call_args[1].get('base_url') == 'https://example.com/v1'


def test_inline_code_calls_extracted_function():
    import pathlib
    content = pathlib.Path('/tmp/hermes-pr-clean-20260721T022500Z/agent/conversation_loop.py').read_text()
    assert '_update_resolved_model_metadata(agent, response)' in content
    assert content.index('def _update_resolved_model_metadata(') > 0
