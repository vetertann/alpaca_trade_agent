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


def test_prompt_has_one_batched_discovery_to_stage_example():
    s = prompt.system_prompt()
    example = s[s.index("# Canonical discovery-to-stage program"):]
    assert "options.enumerate(" in example
    assert "vol.measures(" in example
    assert "sigma=sigma" in example
    assert "vol.evaluate(" in example
    assert "vol.rank(" in example
    assert 'expected_measures = {"lognormal", "block_bootstrap", "student_t"}' in example
    assert "else survivors[0]" not in example
    assert "thesis.open(" in example
    assert example.count("trading.execute(intent)") == 2  # code plus explanation
    assert "Never call `trading.execute` more than once" in s


def test_version_changes_with_the_layer_set():
    assert prompt.prompt_version() != prompt.prompt_version(include_pretrade=True)


def test_version_is_stable_across_calls():
    assert prompt.prompt_version() == prompt.prompt_version()


def test_payload_carries_only_the_bundle():
    p = prompt.payload({"universe": {"SPY": {"spot": 1.0}}})
    assert "SPY" in p and "# Role" not in p


def test_repair_turn_includes_the_hint():
    t = prompt.repair_turn("NameError: x", "check the capability list")
    assert "NameError" in t and "check the capability list" in t
    assert "Do not restart" in t
    assert t.endswith(prompt.OUTPUT_REMINDER)


def test_observation_turn_flags_a_staged_order():
    plain = prompt.observation_turn("out")
    staged = prompt.observation_turn("out", "PASS economics")
    assert "not submitted" not in plain
    assert "not submitted" in staged and "PASS economics" in staged
    assert "this new model program" in staged
    assert plain.endswith(prompt.OUTPUT_REMINDER)
    assert staged.endswith(prompt.OUTPUT_REMINDER)


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
