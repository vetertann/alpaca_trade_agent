import datetime as dt
import pytest
from agent import config


def test_unknown_profile_refused():
    with pytest.raises(config.ConfigError):
        config.profile("whatever")


def test_no_default_profile():
    with pytest.raises(TypeError):
        config.profile()          # name is required -- there is no default


def test_assert_paper_blocks_live():
    config.assert_paper(config.PAPER_TRADING_URL)
    with pytest.raises(config.ConfigError):
        config.assert_paper("https://api.alpaca.markets")


def test_scored_window_boundaries():
    ET = config.ET
    assert not config.in_scored_window(dt.datetime(2026, 8, 31, 9, 29, tzinfo=ET))
    assert config.in_scored_window(dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET))
    assert config.in_scored_window(dt.datetime(2026, 9, 3, 16, 0, tzinfo=ET))
    assert config.in_scored_window(dt.datetime(2026, 9, 4, 9, 29, tzinfo=ET))
    assert not config.in_scored_window(dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET))
    # Friday is outside: the snapshot is at the opening bell.
    assert not config.in_scored_window(dt.datetime(2026, 9, 4, 9, 31, tzinfo=ET))
