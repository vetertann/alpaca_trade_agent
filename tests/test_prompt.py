from agent.brain import prompt


def test_layers_compose_in_order():
    s = prompt.system_prompt()
    assert s.index("# Role") < s.index("# Universe") < s.index("# Output contract")


def test_output_contract_is_the_final_system_section():
    for include_pretrade in (False, True):
        s = prompt.system_prompt(include_pretrade=include_pretrade)
        assert s.rstrip().endswith("text before or after the object.")
        assert s.count("# Output contract") == 1
        assert "confirmation turns include compact PASS/FAIL/N/A verdicts" in s
        blocks = prompt.system_blocks(include_pretrade=include_pretrade)
        assert blocks[-1]["text"] == prompt.OUTPUT_CONTRACT


def test_pretrade_layer_is_conditional():
    """It costs nothing on the cycles that do not trade, which is most of them."""
    assert "Pre-trade check" not in prompt.system_prompt()
    assert "Pre-trade check" in prompt.system_prompt(include_pretrade=True)


def test_capability_contract_documents_discovery_identifiers_and_shapes():
    s = prompt.system_prompt()
    assert "options.enumerate(underlying, exp_gte, exp_lte" in s
    assert "{id, family, underlying, expiry, net_price" in s
    assert "`id` is the candidate identifier" in s
    assert 'status: "awaiting_confirmation"' in s
    assert "premium_at_risk" in s
    assert "vol.implied(price, spot, strike, t_years, option_type)" in s
    assert "15.0 = 15%" in s
    assert "decision.no_trade(reason)" in s
    assert "market.directional_context(symbol)" in s
    assert "risk.direction(candidate_id, sigma, days)" in s
    assert "breakevens" in s and "dollar_delta_per_1pct" in s
    assert "directional_alignment" in s and "expiry_pnl_scenarios" in s


def test_prompt_has_one_batched_discovery_to_stage_example():
    s = prompt.system_prompt()
    example = s[s.index("# Canonical discovery-to-stage program"):]
    assert "options.enumerate(" in example
    assert "options.expiries(" in example
    assert "vol.measures_for(" in example
    assert "sigma=sigma" in example
    assert "vol.evaluate_many(" in example
    assert "vol.rank(" in example
    assert "market.directional_context(" in example
    assert "risk.direction(" in example
    assert 'expected_measures = {"lognormal", "block_bootstrap", "student_t"}' in example
    assert "else survivors[0]" not in example
    assert "thesis.open(" in example
    assert example.count("trading.execute(intent)") == 2  # code plus explanation
    assert "Never call `trading.execute` more than once" in s
    assert "There is no calendar cutoff" in s
    assert "limit=240" in s


def test_canonical_discovery_example_is_valid_python():
    s = prompt.system_prompt()
    section = s[s.index("# Canonical discovery-to-stage program"):]
    source = section.split("```python", 1)[1].split("```", 1)[0]

    compile(source, "<canonical-discovery-example>", "exec")


def test_prompt_separates_volatility_edge_from_directional_evidence():
    s = prompt.system_prompt()
    assert "Volatility edge is not directional evidence" in s
    assert "volatility-led" in s and "direction-led" in s and "mixed" in s
    assert "cap requested risk at" in s and "0.75% of equity" in s
    assert "near-delta-neutral structure has **no tape-" in s
    assert "quote-midpoint direction" in s
    assert "conflicts with current observed" in s
    assert "Treat SPY and QQQ as correlated index exposure" in s


def test_prompt_uses_tournament_sizing_without_relaxing_evidence():
    s = prompt.system_prompt()
    assert "at most 4% of equity maximum loss" in s
    assert "at most **1.5%\nof equity**" in s
    assert "cap an aligned direction-led structure at **3% of equity**" in s
    assert "cap requested risk at **0.75% of equity**" in s
    assert "There is no structure-count allocation target" in s
    assert "below 3.5%" in s
    assert "Do not add to,\naverage down" in s


def test_risk_variant_changes_only_the_rendered_robust_ceiling():
    aggressive = prompt.system_prompt(robust_risk_pct=0.10)
    assert "at most 10% of equity maximum loss" in aggressive
    assert "risk_fraction = (0.1 if robust else" in aggressive
    assert "at most **1.5%\nof equity**" in aggressive
    assert "cap requested risk at **0.75% of equity**" in aggressive
    assert "{{ROBUST_RISK" not in aggressive
    assert prompt.prompt_version(robust_risk_pct=0.10) != prompt.prompt_version()


def test_scenario_variant_is_rendered_everywhere_and_versioned():
    aggressive = prompt.system_prompt(
        include_pretrade=True, scenario_risk_pct=0.10)
    assert "10%-of-equity resulting-" in aggressive
    assert "one-day P&L to 10% of equity" in aggressive
    assert "inside the 10% executable scenario-loss limit" in aggressive
    assert "{{SCENARIO_RISK_PERCENT}}" not in aggressive
    assert prompt.prompt_version(scenario_risk_pct=0.10) != prompt.prompt_version()


def test_risk_variant_cannot_exceed_the_host_single_position_cap():
    import pytest
    with pytest.raises(ValueError, match="robust_risk_pct"):
        prompt.system_prompt(robust_risk_pct=0.16)


def test_scenario_variant_has_a_host_bound():
    import pytest
    with pytest.raises(ValueError, match="scenario_risk_pct"):
        prompt.system_prompt(scenario_risk_pct=0.26)


def test_version_changes_with_the_layer_set():
    assert prompt.prompt_version() != prompt.prompt_version(include_pretrade=True)


def test_version_is_stable_across_calls():
    assert prompt.prompt_version() == prompt.prompt_version()


def test_payload_carries_only_the_bundle():
    p = prompt.payload({"universe": {"SPY": {"spot": 1.0}}})
    assert "SPY" in p and "# Role" not in p


def test_clean_review_contains_host_facts_not_proposal_reasoning():
    review = prompt.review_turn(
        {"clock": {"session_state": "ACTIVE"}},
        {"underlying": "SPY", "legs": []},
        {"direction": {"net_delta": 0.1}}, "PASS host")
    assert "proposal's prior thought and program are deliberately omitted" in review
    assert "canonical_staged_order" in review
    assert "host_recorded_evidence" in review
    assert "confirmation_call" in review
    assert "never switch a staged `execute_if` draft" in review
    assert "PASS host" in review


def test_repair_turn_includes_the_hint():
    t = prompt.repair_turn("NameError: x", "check the capability list")
    assert "NameError" in t and "check the capability list" in t
    assert "Do not restart" in t
    assert t.endswith(prompt.OUTPUT_REMINDER)


def test_last_repair_round_requires_a_terminal_action_not_a_new_draft():
    turn = prompt.repair_turn(
        "ImportError: traceback", "use the allowed modules",
        rounds_remaining=1)
    assert "Exactly one program round remains" in turn
    assert "trading.set_entry_trigger" in turn
    assert "Do not merely stage" in turn


def test_observation_turn_flags_a_staged_order():
    plain = prompt.observation_turn("out")
    staged = prompt.observation_turn("out", "PASS economics")
    assert "not submitted" not in plain
    assert "not submitted" in staged and "PASS economics" in staged
    assert "this new model program" in staged
    assert plain.endswith(prompt.OUTPUT_REMINDER)
    assert staged.endswith(prompt.OUTPUT_REMINDER)


def test_continuation_turn_returns_results_and_preserves_confirmation_budget():
    turn = prompt.continuation_turn('{"simulation": "unstable"}', 2)
    assert '"simulation": "unstable"' in turn
    assert "2 program round(s) remain" in turn
    assert "Do not repeat the same simulation" in turn
    assert "without a terminal submission" in turn
    assert "Repeat the exact same conditional call" in turn
    assert turn.endswith(prompt.OUTPUT_REMINDER)


def test_final_continuation_uses_host_trigger_instead_of_unfinishable_staging():
    turn = prompt.continuation_turn('{"qualified": true}', 1)
    assert "Exactly one program round remains" in turn
    assert "trading.set_entry_trigger" in turn
    assert "Do not merely stage" in turn


def test_state_turn_is_authoritative_and_names_dropped_objects():
    turn = prompt.state_turn({
        "persisted": [{"name": "intent", "type": "dict", "bytes": 120}],
        "dropped": [{"name": "frame", "type": "pandas.DataFrame",
                     "reason": "top-level type is not persisted"}],
        "total_bytes": 120,
    })
    assert "Current persisted program state (authoritative)" in turn
    assert "`intent`: dict" in turn
    assert "`frame`: pandas.DataFrame — top-level type is not persisted" in turn
    assert "Only the names above remain available" in turn
    assert "120" not in turn  # values and byte counts are runtime noise


def test_empty_state_turn_does_not_claim_preloaded_names_are_missing():
    turn = prompt.state_turn(None)
    assert "- (none)" in turn
    assert "Preloaded `obs`, modules, and capability namespaces remain available" in turn


def test_repeat_turn_names_the_repetition_not_just_the_error():
    """Restating the same traceback invites the same program again."""
    t = prompt.repeat_turn("ImportError: import 'time' is not available",
                           "Imports are limited to datetime, json…",
                           failing_line="import time")
    assert "byte-identical" in t
    assert "import time" in t
    assert "Do not re-send the previous program" in t
    assert t.strip().endswith("`thought` and `code`.")


def test_repeat_turn_without_a_located_line():
    t = prompt.repeat_turn("boom", "")
    assert "byte-identical" in t and "offending line" not in t
