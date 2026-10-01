"""Shared pytest fixtures for the 8K analyzer test suite."""
import os
import pytest


@pytest.fixture(autouse=True)
def reset_sec_throttle():
    """Clear fetcher's process-wide SEC rate-limit gate around every test.

    The gate is deliberately global — that's what makes one 429 park every
    other SEC caller in production. In a test run that same statefulness
    leaks: a test that drives a 429 would leave a multi-minute cooldown armed,
    and the next test to touch fetcher would sit in it for real.
    """
    import fetcher

    fetcher._reset_sec_throttle()
    yield
    fetcher._reset_sec_throttle()


@pytest.fixture(autouse=True)
def no_background_refresh(monkeypatch):
    """Rendering a page kicks off daemon threads that refresh stale prices,
    earnings dates and market caps (an API call, then a database write).
    In a test run those threads outlive the test that started them: one
    started by test_pagination finished its write inside test_pg_pool and
    opened a connection through that test's fake pool, failing it whenever
    the API call happened to take ~0.2s. No test relies on the refresh, so
    none starts one."""
    import earnings
    import market_cap
    import stock_price

    for module in (stock_price, earnings, market_cap):
        monkeypatch.setattr(module, "_refresh_in_background", lambda tickers: None)


@pytest.fixture(autouse=True)
def no_volatility_fetch(monkeypatch):
    """The filing page's odds column looks up a year of prices from Yahoo.
    Tests must never reach the network; a test that wants odds patches
    payoff.volatility_for itself."""
    import payoff

    monkeypatch.setattr(payoff, "volatility_for", lambda ticker, timeout=3.0: None)


@pytest.fixture
def tmp_sqlite_db(tmp_path, monkeypatch):
    """Point the app's SQLite DATABASE_PATH at a fresh temp file per test.

    Forces SQLite (not Postgres) by ensuring DATABASE_URL is unset.
    Imports database.py AFTER patching so module-level state picks up the temp path.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_file = tmp_path / "test_filings.db"

    # Patch both the config module and any already-imported reference in database.py
    import config
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(config, "DATABASE_URL", None)

    import database
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_file), raising=False)

    # Initialize schema
    database.initialize_database()
    yield str(db_file)
