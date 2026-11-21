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


def test_ar05_report_templates_are_selected_for_the_current_request(db, execution_factory):
    from app.agent_authority import bind_turn
    from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
    from app.context import assemble
    from app.context_preview import preview
    from app.identity import Identity
    from app.preferences import change_preference
    from app.values import uid

    auth = Identity("U001")
    agent = create_draft(
        db, auth,
        AgentDraft(name="报表事务", model_profile_id="default",
                   tool_grants={"manage_preference": ["list"], "get_rules": ["invoke"]}),
    )
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    set_enabled(db, auth, agent["agent_id"], agent["revision"], True)

    def request(text):
        execution = execution_factory(text)
        with db.transaction() as conn:
            conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
            binding = bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
            conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
        return execution, {**binding, "agent_version": binding["version"]}

    for n in range(1, 21):
        change_preference(
            db, auth, action="remember", scope="C001", key=f"report_{n}",
            value=f"仅适用于第{n}类年度附表：模板{n}须列出核对结果。" + "分录汇总。" * 90,
            request_id=uid(),
        )
    change_preference(
        db, auth, action="remember", scope="C001", key="report_limit",
        value="适用于所有报告：不得外发财务资料。", request_id=uid(),
    )
    config = settings(input_budget=16000, model_id="scripted")
    ordinary, binding = request("一周有几天？")
    payload, manifest, dropped = assemble(db, config, ordinary, [])
    assert not dropped
    assert len(manifest["excluded_preferences"]) == 20
    assert "模板3须" not in payload["messages"][0]["content"]
    assert "不得外发财务资料" in payload["messages"][0]["content"]
    estimate = preview(db, config, auth, ordinary.session_id, "一周有几天？", binding=binding)
    assert estimate["input_estimate"] < estimate["input_limit"]
    report, _ = request("请办理第三类年度附表")
    payload, _, _ = assemble(db, config, report, [])
    state = payload["messages"][0]["content"]
    assert "模板3须" in state and "模板4须" not in state
    assert "不得外发财务资料" in state
