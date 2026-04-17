"""Shared pytest fixtures for the 8K analyzer test suite."""
import os
import pytest


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
