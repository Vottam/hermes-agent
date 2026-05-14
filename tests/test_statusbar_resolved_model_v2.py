from datetime import datetime, timedelta
from types import SimpleNamespace

from cli import HermesCLI


def test_status_bar_uses_resolved_model_and_context_length_v2():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = 'anthropic/@preset/hermes'
    cli_obj.session_start = datetime.now() - timedelta(seconds=30)
    cli_obj._prompt_start_time = None
    cli_obj._prompt_duration = 0.0
    cli_obj.agent = SimpleNamespace(
        model='anthropic/@preset/hermes',
        _resolved_model='minimax/minimax-m2.5',
        _resolved_context_model='minimax/minimax-m2.5',
        _resolved_context_length=128000,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=12345,
            context_length=256000,
            compression_count=1,
        ),
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_api_calls=0,
        get_rate_limit_state=lambda: None,
    )

    snapshot = cli_obj._get_status_bar_snapshot()

    assert snapshot['model_name'] == 'minimax/minimax-m2.5'
    assert snapshot['model_short'] == 'minimax-m2.5'
    assert snapshot['context_length'] == 128000
    assert snapshot['context_percent'] == 10
