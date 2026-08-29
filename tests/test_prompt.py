from agent.brain import prompt


def test_layers_compose_in_order():
    s = prompt.system_prompt()
    assert s.index("# Role") < s.index("# Universe")


def test_pretrade_layer_is_conditional():
    """It costs nothing on the cycles that do not trade, which is most of them."""
    assert "Pre-trade check" not in prompt.system_prompt()
    assert "Pre-trade check" in prompt.system_prompt(include_pretrade=True)


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


def test_observation_turn_flags_a_staged_order():
    plain = prompt.observation_turn("out")
    staged = prompt.observation_turn("out", "PASS economics")
    assert "not submitted" not in plain
    assert "not submitted" in staged and "PASS economics" in staged
