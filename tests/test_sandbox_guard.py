"""The import boundary must admit the analysis stack and refuse the rest."""
import pytest
from agent.sandbox.runner import Sandbox


@pytest.fixture
def sb(tmp_path):
    return Sandbox(lambda ns, fn, a, k: 1.0, workdir=tmp_path)


@pytest.mark.parametrize("code,expect", [
    ("import numpy as np\nprint(np.array([1,2,3]).mean())", "2.0"),
    ("import pandas as pd\nprint(len(pd.DataFrame({'a':[1,2]})))", "2"),
    ("import scipy.stats as st\nprint(round(st.norm.cdf(0),3))", "0.5"),
    ("from scipy.stats import norm\nprint(round(norm.ppf(0.975),3))", "1.96"),
    ("from datetime import timedelta\nprint(timedelta(days=2).days)", "2"),
    ("import math\nprint(math.sqrt(9))", "3.0"),
])
def test_analysis_stack_imports(sb, code, expect):
    """A library's own internal imports must not be caught by the allowlist."""
    r = sb.run(code, {})
    assert r.ok, r.stderr
    assert r.stdout.strip() == expect


@pytest.mark.parametrize("module", ["os", "subprocess", "socket", "shutil",
                                    "importlib", "pathlib", "requests"])
def test_unrelated_modules_are_refused(sb, module):
    r = sb.run(f"import {module}", {})
    assert not r.ok and "not available in the decision runtime" in r.stderr


def test_open_is_not_available(sb):
    """Restricting imports achieves nothing if the program can still open files."""
    r = sb.run("open('/etc/hosts')", {})
    assert not r.ok and "name 'open' is not defined" in r.stderr


def test_credentials_are_not_reachable_from_the_working_directory(sb, tmp_path):
    """The child runs in an empty directory, so a relative path finds nothing."""
    r = sb.run("import numpy\nprint('.env' in str(numpy.__file__))", {})
    assert r.ok
    r2 = sb.run("print(open('.env').read())", {})
    assert not r2.ok


def test_relative_imports_are_refused(sb):
    r = sb.run("from . import something", {})
    assert not r.ok


def test_error_names_what_is_allowed(sb):
    r = sb.run("import socket", {})
    assert "numpy" in r.stderr and "pandas" in r.stderr
