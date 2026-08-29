import pytest
from agent.sandbox.runner import Sandbox, clean_environment, hint_for


def dispatch(ns, fn, args, kwargs):
    if ns == "market" and fn == "bars":
        return [{"c": 100.0}, {"c": 101.0}]
    if ns == "risk" and fn == "max_loss":
        return 265.0
    if ns == "trading" and fn == "execute":
        raise PermissionError("gate refused: zero bid on leg 2")
    raise AttributeError(f"{ns}.{fn} does not exist")


@pytest.fixture
def sb(tmp_path):
    return Sandbox(dispatch, workdir=tmp_path)


def test_program_runs_and_calls_capabilities(sb):
    r = sb.run("bars = market.bars('SPY','1Min','a','b')\nprint(len(bars), bars[-1]['c'])", {})
    assert r.ok, r.stderr
    assert r.stdout.strip() == "2 101.0"
    assert {"ns": "market", "fn": "bars"}.items() <= r.calls[0].items()


def test_obs_is_preloaded(sb):
    r = sb.run("print(obs['universe']['SPY']['spot'])",
               {"universe": {"SPY": {"spot": 769.28}}})
    assert r.ok and r.stdout.strip() == "769.28"


def test_dependent_calls_in_one_program(sb):
    code = ("bars = market.bars('SPY','1Min','a','b')\n"
            "loss = risk.max_loss(bars)\n"
            "print(f'{loss:.0f}')")
    r = sb.run(code, {})
    assert r.ok and r.stdout.strip() == "265"
    assert len(r.calls) == 2


def test_host_refusal_surfaces_as_error(sb):
    r = sb.run("trading.execute({'x': 1})", {})
    assert not r.ok
    assert "gate refused: zero bid" in r.stderr


def test_namespaces_are_read_only(sb):
    r = sb.run("market.bars = 1", {})
    assert not r.ok and "read-only" in r.stderr


def test_unknown_capability_reports_clearly(sb):
    r = sb.run("options.nonexistent()", {})
    assert not r.ok and "does not exist" in r.stderr


@pytest.mark.parametrize("module", ["os", "socket", "subprocess", "pathlib", "builtins"])
def test_unrelated_imports_are_blocked(sb, module):
    r = sb.run(f"import {module}", {})
    assert not r.ok
    assert f"import '{module}' is not available" in r.stderr
    assert "Imports are limited" in hint_for(r.stderr)


def test_promised_imports_remain_available(sb):
    code = ("import math, json, statistics, datetime, time\n"
            "from datetime import timedelta\n"
            "import numpy as np\n"
            "import pandas as pd\n"
            "print(math.sqrt(4), json.dumps([statistics.mean([1, 3])]), "
            "timedelta(days=1).days, np.mean([2, 4]), len(pd.Series([1])), "
            "time.monotonic() > 0)")
    r = sb.run(code, {})
    assert r.ok, r.stderr
    assert r.stdout.strip() == "2.0 [2] 1 3.0 1 True"


def test_scipy_imports_when_dependency_is_installed(sb):
    pytest.importorskip("scipy")
    r = sb.run("from scipy import stats\nprint(round(float(stats.norm.cdf(0)), 1))", {})
    assert r.ok, r.stderr
    assert r.stdout.strip() == "0.5"


def test_clean_environment_strips_secrets(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "x")
    monkeypatch.setenv("PATH_TO_NOTHING", "y")
    env = clean_environment()
    assert "NEBIUS_API_KEY" not in env and "PATH_TO_NOTHING" in env


def test_timeout_is_enforced(tmp_path):
    sb = Sandbox(dispatch, workdir=tmp_path, timeout_s=2.0)
    r = sb.run("while True:\n    pass", {})
    assert r.timed_out and not r.ok


def test_namespace_persists_across_rounds(sb):
    assert sb.run("computed = 42", {}).ok
    r = sb.run("print(computed)", {})
    assert r.ok and r.stdout.strip() == "42"


def test_reset_clears_namespace(sb):
    sb.run("computed = 42", {})
    sb.reset()
    r = sb.run("print(computed)", {})
    assert not r.ok and "NameError" in r.stderr


def test_obs_supports_both_key_and_attribute_access(sb):
    """The prompt teaches obs.universe[...]; a plain dict would burn a whole round."""
    bundle = {"universe": {"SPY": {"spot": 769.28, "iv_rv_ratio": 0.8}},
              "book": [{"symbol": "X", "qty": "1"}]}
    r = sb.run("print(obs.universe['SPY'].spot, obs['universe']['SPY']['iv_rv_ratio'], "
               "obs.book[0].symbol)", bundle)
    assert r.ok, r.stderr
    assert r.stdout.strip() == "769.28 0.8 X"


def test_missing_obs_field_lists_what_exists(sb):
    r = sb.run("obs.nonexistent", {"universe": {}, "book": []})
    assert not r.ok and "Available:" in r.stderr
