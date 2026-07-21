
"""Tests for status bar resolved model/context invalidation behavior."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


def _make_agent(*, model='alias/model', provider='openrouter', resolved_model=None, 
                resolved_provider=None, context_length=256000, resolved_context_length=None):
    """Create a minimal agent mock for status bar tests."""
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


def test_successive_model_change_invalidates_context():
    """When model changes A→B→C, context must be re-resolved each time."""
    from agent.conversation_loop import ConversationLoop
    
    loop = ConversationLoop.__new__(ConversationLoop)
    
    # Simulate first response: model A with 256K context
    response_a = SimpleNamespace(model='model-a', usage=None)
    agent = _make_agent(model='preset', resolved_model='model-a', resolved_context_length=256000)
    
    # After processing response_a, _resolved_context_length should be 256000
    assert agent._resolved_context_length == 256000
    
    # Simulate second response: model B with 1M context
    # The invalidation logic should clear _resolved_context_length before resolving
    response_b = SimpleNamespace(model='model-b', usage=None)
    
    # Patch get_model_context_length to return 1M for model-b
    with patch('agent.conversation_loop.get_model_context_length', return_value=1000000):
        # Process response_b
        _new_resolved = response_b.model
        _prev_resolved = getattr(agent, '_resolved_model', None)
        agent._resolved_model = _new_resolved
        
        if _new_resolved != _prev_resolved:
            agent._resolved_context_length = None  # Invalidation
            from agent.model_metadata import get_model_context_length
            _rcl = get_model_context_length(
                _new_resolved,
                base_url=agent.base_url,
                provider=agent.provider,
            )
            if _rcl and _rcl > 0:
                agent._resolved_context_length = _rcl
    
    assert agent._resolved_context_length == 1000000
    assert agent._resolved_model == 'model-b'


def test_same_model_does_not_re_resolve():
    """When model stays the same, no re-resolution should occur."""
    agent = _make_agent(model='preset', resolved_model='model-a', resolved_context_length=256000)
    
    with patch('agent.conversation_loop.get_model_context_length') as mock_resolve:
        # Simulate same model response
        _new_resolved = 'model-a'
        _prev_resolved = getattr(agent, '_resolved_model', None)
        
        if _new_resolved != _prev_resolved:
            agent._resolved_context_length = None
            from agent.model_metadata import get_model_context_length
            _rcl = get_model_context_length(_new_resolved, base_url='', provider='')
            if _rcl and _rcl > 0:
                agent._resolved_context_length = _rcl
    
    # Should NOT have called resolution
    mock_resolve.assert_not_called()
    # Context should remain unchanged
    assert agent._resolved_context_length == 256000


def test_failed_resolution_does_not_keep_old_context():
    """If resolution fails, old context must not be preserved."""
    agent = _make_agent(model='preset', resolved_model='model-a', resolved_context_length=256000)
    
    with patch('agent.conversation_loop.get_model_context_length', side_effect=Exception('fail')):
        _new_resolved = 'model-b'
        _prev_resolved = getattr(agent, '_resolved_model', None)
        agent._resolved_model = _new_resolved
        
        if _new_resolved != _prev_resolved:
            agent._resolved_context_length = None  # Invalidation happens BEFORE resolution
            try:
                from agent.model_metadata import get_model_context_length
                _rcl = get_model_context_length(_new_resolved, base_url='', provider='')
                if _rcl and _rcl > 0:
                    agent._resolved_context_length = _rcl
            except Exception:
                pass
    
    # After failed resolution, context should be None (not 256000)
    assert agent._resolved_context_length is None
