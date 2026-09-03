"""Tests for the signal-first database layer: the allow-listed field writer,
the ingest watermark, labels, guidelines, and the company-context cache."""
import pytest

import database


def _insert(accession="0001-26-000001", **overrides):
    """Insert a minimal filing and return its id."""
    row = {
        "accession_no": accession,
        "company": "Acme Corp",
        "ticker": "ACME",
        "cik": "0001234567",
        "filed_date": "2026-09-01",
        "item_codes": "5.02",
        "summary": "Something happened",
        "auto_category": "Management Change",
        "filing_url": "https://example.com",
        "raw_text": "text",
        "matched_keywords": "",
    }
    row.update(overrides)
    database.insert_filing(row)
    return database.get_filing_by_accession(accession)["id"]


# ---------------------------------------------------------------------------
# update_filing_fields
# ---------------------------------------------------------------------------

def test_update_filing_fields_writes_a_subset(tmp_sqlite_db):
    filing_id = _insert()
    database.update_filing_fields(
        filing_id,
        signal_types="FORFEITURE_EXIT,NO_SUCCESSOR",
        signal_score=8,
        triage_verdict="DEEP_LOOK",
    )
    row = database.get_filing_by_id(filing_id)
    assert row["signal_types"] == "FORFEITURE_EXIT,NO_SUCCESSOR"
    assert row["signal_score"] == 8
    assert row["triage_verdict"] == "DEEP_LOOK"
    # Untouched columns keep their values
    assert row["company"] == "Acme Corp"


def test_update_filing_fields_rejects_unknown_columns(tmp_sqlite_db):
    """A typo must crash, not silently write nothing. A backfill that skips
    every row while reporting success is the worst possible failure here."""
    filing_id = _insert()
    with pytest.raises(ValueError, match="unknown column"):
        database.update_filing_fields(filing_id, signl_score=5)


def test_update_filing_fields_with_no_fields_is_a_noop(tmp_sqlite_db):
    filing_id = _insert()
    assert database.update_filing_fields(filing_id) == 0


# ---------------------------------------------------------------------------
# app_status watermark
# ---------------------------------------------------------------------------

def test_app_status_roundtrip(tmp_sqlite_db):
    assert database.get_app_status("ingested_through") is None
    assert database.get_app_status("ingested_through", "2026-01-01") == "2026-01-01"

    database.set_app_status("ingested_through", "2026-09-01")
    assert database.get_app_status("ingested_through") == "2026-09-01"

    # Upsert, not insert — a second write replaces rather than duplicating
    database.set_app_status("ingested_through", "2026-09-02")
    assert database.get_app_status("ingested_through") == "2026-09-02"


# ---------------------------------------------------------------------------
# judgments
# ---------------------------------------------------------------------------

def test_judgment_upsert_and_read(tmp_sqlite_db):
    filing_id = _insert()
    assert database.upsert_judgment(filing_id, "signal", note="real forfeiture") is True

    judgment = database.get_judgment(filing_id)
    assert judgment["label"] == "signal"
    assert judgment["note"] == "real forfeiture"
    assert judgment["source"] == "review_ui"


def test_relabeling_replaces_rather_than_appends(tmp_sqlite_db):
    filing_id = _insert()
    database.upsert_judgment(filing_id, "signal")
    database.upsert_judgment(filing_id, "noise", source="digest_link")

    assert database.get_judgment(filing_id)["label"] == "noise"
    assert database.count_judgments() == {"noise": 1}


def test_invalid_label_is_rejected(tmp_sqlite_db):
    """A stale or tampered email link must not be able to write junk into the
    training set."""
    filing_id = _insert()
    assert database.upsert_judgment(filing_id, "AMAZING") is False
    assert database.get_judgment(filing_id) is None


def test_delete_judgment_powers_undo(tmp_sqlite_db):
    filing_id = _insert()
    database.upsert_judgment(filing_id, "noise")
    assert database.delete_judgment(filing_id) == 1
    assert database.get_judgment(filing_id) is None


def test_watchlist_seed_is_idempotent(tmp_sqlite_db):
    starred = _insert("0001-26-000001")
    _insert("0001-26-000002")  # not starred
    database.add_to_watchlist(starred)

    assert database.seed_judgments_from_watchlist() == 1
    assert database.get_judgment(starred)["source"] == "watchlist_seed"
    # Second run finds nothing new and doesn't overwrite a later manual label
    database.upsert_judgment(starred, "noise", source="review_ui")
    assert database.seed_judgments_from_watchlist() == 0
    assert database.get_judgment(starred)["label"] == "noise"


def test_labeled_examples_prefer_matching_signal_type(tmp_sqlite_db):
    forfeiture = _insert("0001-26-000001")
    grant = _insert("0001-26-000002")
    database.update_filing_fields(forfeiture, signal_types="FORFEITURE_EXIT,NO_SUCCESSOR")
    database.update_filing_fields(grant, signal_types="HURDLE_CONVICTION")
    database.upsert_judgment(forfeiture, "signal")
    database.upsert_judgment(grant, "noise")

    examples = database.get_labeled_examples(signal_type="FORFEITURE_EXIT")
    assert [e["id"] for e in examples] == [forfeiture]

    positives = database.get_labeled_examples(label="noise")
    assert [e["id"] for e in positives] == [grant]


# ---------------------------------------------------------------------------
# guidelines
# ---------------------------------------------------------------------------

def test_guidelines_roundtrip_and_deactivate(tmp_sqlite_db):
    database.add_guideline("Ignore SPAC director shuffles.")
    database.add_guideline("Always surface CFO exits under 18 months tenure.")

    rules = database.get_guidelines()
    assert len(rules) == 2

    database.deactivate_guideline(rules[0]["id"])
    assert len(database.get_guidelines()) == 1
    assert len(database.get_guidelines(active_only=False)) == 2


# ---------------------------------------------------------------------------
# company profiles
# ---------------------------------------------------------------------------

def test_company_profile_upsert_and_fetch(tmp_sqlite_db):
    database.upsert_company_profile("0001234567", ticker="ACME", market_cap=310_000_000,
                                    price=8.40, next_earnings="2026-09-14")
    profile = database.get_company_profile("0001234567")
    assert profile["ticker"] == "ACME"
    assert profile["market_cap"] == 310_000_000

    # Refresh updates in place rather than duplicating the CIK
    database.upsert_company_profile("0001234567", price=9.10)
    assert database.get_company_profile("0001234567")["price"] == 9.10


def test_company_profile_rejects_unknown_field(tmp_sqlite_db):
    with pytest.raises(ValueError, match="unknown field"):
        database.upsert_company_profile("0001234567", markt_cap=5)


def test_missing_company_profile_returns_none(tmp_sqlite_db):
    assert database.get_company_profile("9999999999") is None
    assert database.get_company_profile("") is None


# ---------------------------------------------------------------------------
# FORM4 rows must stay out of the 8-K repair queues
# ---------------------------------------------------------------------------

def test_form4_rows_excluded_from_missing_text_repair(tmp_sqlite_db):
    """FORM4 rows have no 8-K document. If they showed up here, 'Retry Missing
    Summaries' would try to fetch them forever and the stranded count would
    never reach zero."""
    _insert("0001-26-000001", raw_text="")                     # real stranded 8-K
    _insert("0001-26-000002", raw_text="", source="FORM4")     # not an 8-K

    stranded = database.get_filings_missing_text()
    assert [f["accession_no"] for f in stranded] == ["0001-26-000001"]
    assert database.count_filings_missing_text() == 1


def test_form4_rows_excluded_from_resummarize(tmp_sqlite_db):
    _insert("0001-26-000001", raw_text="8-K body")
    _insert("0001-26-000002", raw_text="Form 4 rendering", source="FORM4")

    rows = database.get_filings_for_resummarize("2026-08-01", "2026-09-30")
    assert [f["accession_no"] for f in rows] == ["0001-26-000001"]


def test_legacy_rows_with_null_source_are_still_repaired(tmp_sqlite_db):
    """Every row that existed before this rebuild has source NULL (the column
    was added by migration, and ALTER TABLE backfills NULL). The COALESCE has
    to keep those thousands of rows in scope."""
    import sqlite3

    filing_id = _insert("0001-26-000001", raw_text="")
    # Simulate a pre-migration row: the new-row default is '8-K', but legacy
    # rows genuinely hold NULL.
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET source = NULL WHERE id = ?", (filing_id,))
    conn.commit()
    conn.close()

    assert database.get_filing_by_id(filing_id)["source"] is None
    assert database.count_filings_missing_text() == 1
    assert len(database.get_filings_missing_text()) == 1
