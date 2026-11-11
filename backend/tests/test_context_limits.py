from app.config import Settings
from app.model_connection import ConnectionInput, _candidate


def settings(**values):
    return Settings(_env_file=None, model_context_limit=1000000, **values)


def test_project_window_and_explicit_legacy_cap():
    limits = settings().context_limits
    assert (limits["input_limit"], limits["effective_window"], limits["trigger_input"]) == (
        92952,
        100000,
        82952,
    )
    limited = settings(input_budget=16000).context_limits
    assert (limited["input_limit"], limited["effective_window"], limited["trigger_input"]) == (
        16000,
        23048,
        13696,
    )


def test_smaller_model_window():
    config = Settings(_env_file=None, model_context_limit=32000)
    assert config.context_budget == 27904
    assert config.context_limits["trigger_input"] == 24704


def test_switch_to_unconfirmed_model_keeps_a_conservative_budget():
    base = settings(model_base_url="https://api.example.com", model_id="first", model_api_key="key")
    switched = _candidate(
        base,
        ConnectionInput(base_url="https://api.example.com", model_id="other", api_key="key"),
    )
    assert switched.model_configured
    assert switched.context_limits["window_status"] == "unknown"
    assert switched.context_budget <= 16000


def test_cache_metrics_never_treat_missing_usage_as_zero():
    from app.insights import _cache_usage

    assert _cache_usage([{"usage": None}])["hit_tokens"] is None
    result = _cache_usage(
        [
            {
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 75,
                    "prompt_cache_miss_tokens": 25,
                }
            },
            {"usage": {"prompt_tokens": 80, "prompt_tokens_details": {"cached_tokens": 40}}},
            {"usage": None},
        ]
    )
    assert result == {
        "known_calls": 2,
        "hit_tokens": 115,
        "miss_tokens": 65,
        "hit_ratio": 115 / 180,
    }
