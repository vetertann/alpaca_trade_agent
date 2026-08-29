"""Test isolation.

Provider resolution reads os.environ. Any test that loads a real .env, or any
developer shell that exports keys, silently changes which model a chain resolves
to — so tests that assert on resolution become order-dependent and fail only in
some runs. Every test starts from a known-empty credential environment instead.
"""
import pytest

CREDENTIAL_VARS = (
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_ACCOUNT_ID",
    "DEV_ALPACA_API_KEY", "DEV_ALPACA_SECRET_KEY", "DEV_ALPACA_ACCOUNT_ID",
    "ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
    "OPENAI_API_KEY", "OPEN_AI_API_KEY",
    "NEBIUS_API_KEY", "FEATHERLESS_API_KEY", "FEATHERLESS_KEY",
    "SECRET", "ALPACA_SECRET", "COLLECTOR_HOST",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_CAPTURE_CONTENT",
)


@pytest.fixture(autouse=True)
def _clean_credentials(monkeypatch):
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
