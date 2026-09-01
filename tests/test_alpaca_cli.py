import json
from types import SimpleNamespace

import pytest

from agent.config import Profile
from agent.host.alpaca_cli import AlpacaCLI, CLIError
from agent.host.execution import Executor
from agent.host.rest import Rest


PROFILE = Profile("dev", "PKTEST", "very-secret", "account-1")


def test_cli_uses_stdin_and_environment_without_secret_arguments(monkeypatch):
    seen = {}
    monkeypatch.setenv("ALPACA_DEBUG", "true")

    def run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"id":"order-1"}', stderr="")

    monkeypatch.setattr("agent.host.alpaca_cli.shutil.which",
                        lambda value: "/usr/local/bin/alpaca")
    monkeypatch.setattr("agent.host.alpaca_cli.subprocess.run", run)
    cli = AlpacaCLI(PROFILE)
    result = cli.request("POST", "/v2/orders", body={"qty": "1"})

    assert result == {"id": "order-1"}
    assert seen["command"][:4] == ["/usr/local/bin/alpaca", "api", "POST", "/v2/orders"]
    assert "very-secret" not in " ".join(seen["command"])
    assert json.loads(seen["input"]) == {"qty": "1"}
    assert seen["env"]["ALPACA_API_KEY"] == "PKTEST"
    assert seen["env"]["ALPACA_SECRET_KEY"] == "very-secret"
    assert seen["env"]["ALPACA_LIVE_TRADE"] == "false"
    assert "ALPACA_DEBUG" not in seen["env"]


def test_cli_exposes_structured_http_status(monkeypatch):
    monkeypatch.setattr("agent.host.alpaca_cli.shutil.which",
                        lambda value: "/usr/local/bin/alpaca")
    monkeypatch.setattr(
        "agent.host.alpaca_cli.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="",
            stderr='{"error":"missing","status":404,"code":40410000}'))
    cli = AlpacaCLI(PROFILE)

    with pytest.raises(CLIError) as raised:
        cli.request("GET", "/v2/orders:by_client_order_id",
                    query="client_order_id=missing")
    assert raised.value.status_code == 404
    assert raised.value.payload["error"] == "missing"


def test_rest_routes_execution_calls_through_cli():
    calls = []

    class FakeCLI:
        def request(self, method, path, *, body=None, query=None):
            calls.append((method, path, body, query))
            return {"ok": True}

    rest = Rest.__new__(Rest)
    rest._cli = FakeCLI()
    rest.submit_order_body({"client_order_id": "a one", "qty": "1"})
    rest.order_by_client_order_id("a one")
    rest.cancel("order-1")

    assert calls == [
        ("POST", "/v2/orders", {"client_order_id": "a one", "qty": "1"}, None),
        ("GET", "/v2/orders:by_client_order_id", None,
         "client_order_id=a+one"),
        ("DELETE", "/v2/orders/order-1", None, None),
    ]


def test_executor_understands_cli_duplicate_error():
    error = CLIError(
        "client_order_id must be unique", status_code=422,
        payload={"code": 42210000, "message": "client_order_id must be unique"})
    assert Executor._http_status(error) == 422
    assert Executor._duplicate_client_id(error)
