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
