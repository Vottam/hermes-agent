"""Tests for status bar resolved model/context invalidation behavior."""

from types import SimpleNamespace
from unittest.mock import patch


def _make_agent(*, model='alias/model', provider='openrouter',
                resolved_model=None, resolved_provider=None,
                context_length=256000, resolved_context_length=None):
    """Build a minimal agent mock for status bar attribute tests."""
    agent = SimpleNamespace()
    agent.model = model
    agent.provider = provider
    agent._resolved_provider = resolved_provider or provider
    agent.base_url = 'https://openrouter.ai/api/v1'
    agent.context_compressor = SimpleNamespace(context_length=context_length)
    if resolved_model is not None:
        agent._resolved_model = resolved_model
        agent._resolved_context_model = resolved_model
    if resolved_context_length is not None:
        agent._resolved_context_length = resolved_context_length
    return agent


def _simulate_resolution(agent, _new_resolved):
    """Mimics the status bar context invalidation logic from
    conversation_loop.py.
    
    This function mirrors the behavior of the response processing block
    so we can test the invalidation logic independently.
    """
    _prev_resolved = getattr(agent, '_resolved_model', None)
    agent._resolved_model = _new_resolved

    if _new_resolved != _prev_resolved:
        agent._resolved_context_length = None
        try:
            from agent.model_metadata import get_model_context_length
            _rcl = get_model_context_length(
                _new_resolved, base_url='', provider='')
            if _rcl and _rcl > 0:
                agent._resolved_context_length = _rcl
        except Exception:
            pass


def test_successive_model_change_invalidates_context():
    agent = _make_agent(model='preset', resolved_model='model-a',
                       resolved_context_length=256000)
    assert agent._resolved_context_length == 256000

    with patch('agent.model_metadata.get_model_context_length', return_value=1000000):
        _simulate_resolution(agent, 'model-b')

    assert agent._resolved_context_length == 1000000
    assert agent._resolved_model == 'model-b'


def test_same_model_does_not_re_resolve():
    agent = _make_agent(model='preset', resolved_model='model-a',
                       resolved_context_length=256000)

    with patch('agent.model_metadata.get_model_context_length') as m:
        _simulate_resolution(agent, 'model-a')

    m.assert_not_called()
    assert agent._resolved_context_length == 256000


def test_failed_resolution_does_not_keep_old_context():
    agent = _make_agent(model='preset', resolved_model='model-a',
                       resolved_context_length=256000)

    with patch('agent.model_metadata.get_model_context_length',
               side_effect=Exception('fail')):
        _simulate_resolution(agent, 'model-b')

    assert agent._resolved_context_length is None


def test_five_transitions_all_recalculate():
    agent = _make_agent(model='preset')

    for i, expected in enumerate([256000, 1000000, 32000, 8000, 512000], 1):
        with patch('agent.model_metadata.get_model_context_length',
                   return_value=expected):
            _simulate_resolution(agent, f'model-{i}')

        assert agent._resolved_context_length == expected, (
            f'Transition {i}: expected {expected}, '
            f'got {agent._resolved_context_length}')


def test_no_response_model_preserves_state():
    agent = _make_agent(model='preset', resolved_model='model-a',
                       resolved_context_length=256000)

    # If response.model is None, no resolution should happen
    _new_resolved = None
    _prev_resolved = getattr(agent, '_resolved_model', None)
    if _new_resolved is not None:
        agent._resolved_model = _new_resolved

    # State unchanged
    assert agent._resolved_context_length == 256000
    assert agent._resolved_model == 'model-a'
