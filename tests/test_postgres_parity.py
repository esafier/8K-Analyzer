"""PostgreSQL parity tests — the ones SQLite cannot catch.

The rest of the suite runs against SQLite, which hides every dual-dialect
difference this codebase actually trips over: `?` versus `%s`, RETURNING,
INTERVAL syntax, and rows arriving as plain tuples instead of sqlite3.Row
(so `.get()` works on one engine and raises on the other — a bug this
project has shipped more than once, and the reason CLAUDE.md has a rule
about it).

Skipped entirely unless DATABASE_URL points at a real Postgres, so the local
suite is unaffected. CI runs this against a postgres:16 service container.
"""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres parity tests need DATABASE_URL",
)


@pytest.fixture(scope="module")
def pg():
    """Boot the schema on Postgres and hand back the database module."""
    import database

    if not database._using_postgres():
        pytest.skip("DATABASE_URL is set but pg8000 is unavailable")

    # Start from a clean slate so a re-run isn't polluted by the last one.
    conn = database.get_connection()
    cursor = conn.cursor()
    for table in ("judgments", "watchlist", "digests", "guidelines",
                  "company_profiles", "departure_extractions", "filings",
                  "backfill_runs", "app_status", "market_caps",
                  "earnings_cache", "stock_prices"):
        cursor.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    conn.commit()
    conn.close()

    database.initialize_database()
    return database


def _insert(db, accession="pg-1", **overrides):
    row = {
        "accession_no": accession, "company": "Postgres Corp", "ticker": "PGSQL",
        "cik": "0001234567", "filed_date": "2026-09-02", "item_codes": "5.02",
        "summary": "CFO resigned.", "auto_category": "Management Change",
        "filing_url": "https://sec.gov/i.htm", "raw_text": "text",
        "matched_keywords": "",
    }
    row.update(overrides)
    db.insert_filing(row)
    return db.get_filing_by_accession(accession)["id"]


def test_migrations_run_and_every_new_column_exists(pg):
    conn = pg.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT column_name FROM information_schema.columns "
                   "WHERE table_name = 'filings'")
    columns = {r[0] for r in cursor.fetchall()}
    conn.close()

    for column in ("source", "signals_json", "signal_types", "judge_json",
                   "context_json", "pipeline_version", "legacy_triage_json",
                   "price_at_ingest", "market_cap_at_ingest", "accepted_at",
                   "judged_at"):
        assert column in columns, f"migration missed {column}"


def test_insert_and_read_back_a_filing(pg):
    filing_id = _insert(pg, "pg-insert")
    row = pg.get_filing_by_id(filing_id)
    assert row["company"] == "Postgres Corp"
    # Real dict on Postgres — .get() must work (CLAUDE.md compatibility rule)
    assert dict(row).get("source") == "8-K"


def test_update_filing_fields_uses_the_right_placeholders(pg):
    filing_id = _insert(pg, "pg-update")
    pg.update_filing_fields(filing_id, signal_score=8, triage_verdict="DEEP_LOOK",
                            signal_types="FORFEITURE_EXIT")
    row = pg.get_filing_by_id(filing_id)
    assert row["signal_score"] == 8
    assert row["signal_types"] == "FORFEITURE_EXIT"


def test_judgments_upsert_uses_on_conflict(pg):
    """SQLite takes INSERT OR REPLACE; Postgres needs ON CONFLICT with the
    assignment list repeated. Two different statements, one behaviour."""
    filing_id = _insert(pg, "pg-judge")
    pg.upsert_judgment(filing_id, "signal", note="first")
    pg.upsert_judgment(filing_id, "noise", note="second")

    judgment = pg.get_judgment(filing_id)
    assert judgment["label"] == "noise"
    assert judgment["note"] == "second"
    assert pg.count_judgments() == {"noise": 1}


def test_company_profile_upsert(pg):
    pg.upsert_company_profile("0009999999", ticker="PGSQL", market_cap=310_000_000, price=8.4)
    pg.upsert_company_profile("0009999999", price=9.1)
    profile = pg.get_company_profile("0009999999")
    assert profile["price"] == 9.1
    assert profile["market_cap"] == 310_000_000


def test_company_profile_ttl_uses_postgres_interval_syntax(pg):
    """The stale-row filter is written per dialect — NOW() - INTERVAL 'n hours'
    against datetime('now', '-n hours'). Only Postgres can catch a mistake in
    the first one."""
    pg.upsert_company_profile("0008888888", ticker="TTL", price=1.0)
    assert pg.get_company_profile("0008888888", max_age_hours=24) is not None
    assert pg.get_company_profile("0008888888", max_age_hours=None) is not None


def test_app_status_upsert(pg):
    pg.set_app_status("ingested_through", "2026-09-01")
    pg.set_app_status("ingested_through", "2026-09-02")
    assert pg.get_app_status("ingested_through") == "2026-09-02"


def test_inbox_query_runs_with_all_filters(pg):
    filing_id = _insert(pg, "pg-inbox", triage_verdict="DEEP_LOOK", signal_score=8,
                        signal_direction="BEARISH", signal_types="FORFEITURE_EXIT",
                        top_signal="CFO forfeits unvested comp.")
    rows = pg.get_inbox_filings(days=3650, min_score=5, direction="BEARISH",
                                signal_type="FORFEITURE_EXIT", unlabeled_only=True)
    assert any(r["id"] == filing_id for r in rows)
    assert pg.count_inbox_filings(days=3650, min_score=5) >= 1


def test_review_queue_and_signal_counts(pg):
    _insert(pg, "pg-review", triage_verdict="MONITOR", signal_score=6,
            signal_direction="BEARISH", signal_types="NO_SUCCESSOR")
    assert pg.count_review_queue() >= 1
    assert pg.get_review_queue(limit=1)
    assert "NO_SUCCESSOR" in pg.get_signal_type_counts(days=3650)


def test_create_backfill_run_uses_returning(pg):
    """Postgres needs RETURNING id; SQLite uses cursor.lastrowid."""
    run_id = pg.create_backfill_run("test", "2026-09-01", "2026-09-02", "model")
    assert isinstance(run_id, int)
    pg.complete_backfill_run(run_id, fetched=1, filtered=1, new=1, skipped=0)
    assert any(r["id"] == run_id for r in pg.get_recent_backfill_runs())


def _age_run(pg, run_id, hours):
    """Backdate a run's start — Postgres INTERVAL, which SQLite cannot test."""
    conn = pg.get_connection()
    cursor = conn.cursor()
    p = pg._placeholder()
    cursor.execute(
        f"UPDATE backfill_runs SET started_at = NOW() - INTERVAL '{hours} hours' "
        f"WHERE id = {p}",
        (run_id,),
    )
    conn.commit()
    conn.close()


def _status(pg, run_id):
    conn = pg.get_connection()
    cursor = conn.cursor()
    p = pg._placeholder()
    cursor.execute(f"SELECT status FROM backfill_runs WHERE id = {p}", (run_id,))
    status = cursor.fetchone()[0]
    conn.close()
    return status


def test_boot_does_not_reap_a_live_run(pg):
    """Every entry point boots the schema; several run against this database
    at once. Reaping unconditionally marked a live Actions backfill failed."""
    run_id = pg.create_backfill_run("gap_backfill", "2026-08-20", "2026-09-03", "default")

    pg.initialize_database()

    assert _status(pg, run_id) == "running"


def test_boot_reaps_a_long_abandoned_run(pg):
    run_id = pg.create_backfill_run("gap_backfill", "2026-08-20", "2026-09-03", "default")
    _age_run(pg, run_id, pg.STUCK_BACKFILL_HOURS + 1)

    pg.initialize_database()

    assert _status(pg, run_id) == "failed"


def test_form4_rows_are_excluded_from_repair_queries(pg):
    _insert(pg, "pg-8k", raw_text="")
    _insert(pg, "pg-f4", raw_text="", source="FORM4")
    accessions = {r["accession_no"] for r in pg.get_filings_missing_text()}
    assert "pg-8k" in accessions
    assert "pg-f4" not in accessions


def test_digest_records_round_trip(pg):
    filing_id = _insert(pg, "pg-digest")
    pg.record_digest("email", [filing_id])
    assert filing_id in pg.get_recently_digested_ids(days=7)


def test_labeled_examples_query(pg):
    filing_id = _insert(pg, "pg-example", signal_types="FORFEITURE_EXIT",
                        signal_score=8, triage_verdict="DEEP_LOOK",
                        top_signal="Forfeiture.")
    pg.upsert_judgment(filing_id, "signal", note="real one")
    examples = pg.get_labeled_examples(signal_type="FORFEITURE_EXIT")
    assert examples and examples[0]["note"] == "real one"


def test_pipeline_persist_against_postgres(pg):
    """The full write path, including the legacy_triage_json snapshot."""
    import pipeline
    from unittest.mock import patch

    filing_id = _insert(pg, "pg-pipeline", triage_verdict="MONITOR", signal_score=4,
                        top_signal="old line")

    facts = {
        "relevant": True, "top_level_category": "Management Change",
        "subcategories": ["CFO Departure"], "reasoning": "one event",
        "departures": [{"name": "Jane Doe", "title": "CFO", "role_class": "CFO",
                        "effective_immediately": True, "days_notice": 0,
                        "stated_reason": "resigned", "is_retirement": False,
                        "is_merger_related": False, "successor_named": False,
                        "successor_info": "search underway",
                        "forfeiture_flag": "forfeited",
                        "comp_impact": "forfeits ~$4.2M"}],
        "appointments": [], "comp_events": [], "insider_transactions": [],
        "other": [], "filing_flags": {}, "_tokens_in": 10, "_tokens_out": 5,
    }
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._build_context", return_value={"price": 10.0}), \
         patch("pipeline._judge", return_value=None):
        result = pipeline.analyze_filing(pg.get_filing_by_id(filing_id))

    pipeline.persist(filing_id, result)
    row = pg.get_filing_by_id(filing_id)
    assert "FORFEITURE_EXIT" in row["signal_types"]
    assert json.loads(row["legacy_triage_json"])["signal_score"] == 4


def test_outcome_lifecycle_on_postgres(pg):
    """INSERT ... ON CONFLICT DO NOTHING and the dynamic price_<n> columns
    differ from SQLite's INSERT OR IGNORE — exercise the whole round trip."""
    filing_id = _insert(pg, "pg-outcome", triage_verdict="MONITOR", signal_score=6,
                        signal_direction="BEARISH", signal_types="FORFEITURE_EXIT",
                        pipeline_version="v4.2-signals", price_at_ingest=10.0,
                        filed_date="2026-06-01")

    assert any(r["id"] == filing_id for r in pg.get_filings_needing_baseline())
    pg.insert_outcome_baseline(filing_id, "PGSQL", "BEARISH", "FORFEITURE_EXIT",
                               "2026-06-01", 10.0, 500.0)
    pg.insert_outcome_baseline(filing_id, "PGSQL", "BEARISH", "FORFEITURE_EXIT",
                               "2026-06-01", 99.0, 999.0)   # duplicate: ignored

    due = pg.get_outcomes_due(7, "2026-06-10")
    assert len(due) == 1
    pg.mark_outcome(due[0]["id"], 7, 9.0, 505.0)

    row = next(r for r in pg.get_all_outcomes() if r["filing_id"] == filing_id)
    assert row["price_0"] == 10.0
    assert row["price_7"] == 9.0
    assert pg.get_outcomes_due(7, "2026-06-10") == []
