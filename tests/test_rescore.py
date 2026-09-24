"""Tests for rescore.py — re-ranking stored filings with no model calls.

The point of rescoring is that a detector fix reaches every filing already in
the database for free. That only works if it re-derives signals from the
stored facts and context, and if a judgment formed in response to signals
that no longer exist is dropped rather than left propping up the rank.
"""
import json

import database
import rescore


def _add(accession, structured, context=None, judge=None, verdict="MONITOR", score=6):
    database.insert_filing({
        "accession_no": accession, "company": f"Co {accession}", "ticker": "AAA",
        "cik": "1", "filed_date": "2026-09-08", "item_codes": "5.02",
        "filing_url": "u", "raw_text": "t",
        "structured_summary": json.dumps(structured),
        "context_json": json.dumps(context or {}),
        "judge_json": json.dumps(judge) if judge else None,
        "triage_verdict": verdict, "signal_score": score,
        "signal_direction": "BEARISH", "top_signal": "old line",
        "pipeline_version": "v4.1-signals",
    })
    return database.get_filing_by_accession(accession)["id"]


APPOINTMENT_ONLY = {
    "departures": [], "comp_events": [], "insider_transactions": [], "other": [],
    "appointments": [{"name": "D Goldschmidt", "title": "Director", "role_class": "DIRECTOR"}],
    "filing_flags": {},
}

FORFEITURE = {
    "departures": [{"name": "Jane Doe", "title": "CFO", "role_class": "CFO",
                    "effective_immediately": True, "days_notice": 0,
                    "stated_reason": "resigned", "is_retirement": False,
                    "is_merger_related": False, "successor_named": False,
                    "successor_info": "search underway", "forfeiture_flag": "forfeited"}],
    "appointments": [], "comp_events": [], "insider_transactions": [], "other": [],
    "filing_flags": {},
}


def test_cluster_only_filing_drops_to_pass(tmp_sqlite_db):
    """The week-one pattern: MONITOR 6 on nothing but company history."""
    filing_id = _add("a-1", APPOINTMENT_ONLY, context={"departures_24mo": 5},
                     judge={"score": 5, "verdict": "MONITOR", "direction": "BEARISH",
                            "thesis": "Adds a director amid five departures."})
    rescore.run(verbose=False)

    row = database.get_filing_by_id(filing_id)
    assert row["triage_verdict"] == "PASS"
    assert row["signal_score"] == 0
    assert row["judge_json"] is None  # the judgment answered signals that no longer exist


def test_real_signal_keeps_its_judgment(tmp_sqlite_db):
    judge = {"score": 8, "verdict": "DEEP_LOOK", "direction": "BEARISH",
             "thesis": "CFO walks from $4.2M unvested.", "disputed_signals": []}
    filing_id = _add("a-1", FORFEITURE, judge=judge, verdict="DEEP_LOOK", score=8)
    rescore.run(verbose=False)

    row = database.get_filing_by_id(filing_id)
    assert row["triage_verdict"] == "DEEP_LOOK"
    assert row["signal_score"] == 8
    assert row["top_signal"] == "CFO walks from $4.2M unvested."
    assert "FORFEITURE_EXIT" in row["signal_types"]


def test_rescore_stamps_the_current_pipeline_version(tmp_sqlite_db):
    from config import PIPELINE_VERSION
    filing_id = _add("a-1", FORFEITURE)
    rescore.run(verbose=False)
    assert database.get_filing_by_id(filing_id)["pipeline_version"] == PIPELINE_VERSION


def test_dry_run_writes_nothing(tmp_sqlite_db):
    filing_id = _add("a-1", APPOINTMENT_ONLY, context={"departures_24mo": 5})
    result = rescore.run(dry_run=True, verbose=False)

    assert result["changed"] == 1
    assert database.get_filing_by_id(filing_id)["triage_verdict"] == "MONITOR"


def test_rows_without_stored_facts_are_skipped(tmp_sqlite_db):
    database.insert_filing({
        "accession_no": "legacy", "company": "Old", "ticker": "AAA", "cik": "1",
        "filed_date": "2026-09-08", "item_codes": "5.02", "filing_url": "u",
        "raw_text": "t", "pipeline_version": "v4.1-signals",
    })
    assert rescore.run(verbose=False)["rows"] == 0


def test_rescore_makes_no_model_calls(tmp_sqlite_db, monkeypatch):
    """The entire reason this exists is that it is free."""
    def boom(*a, **k):
        raise AssertionError("rescore called a model")

    monkeypatch.setattr("llm.classify_and_summarize", boom)
    monkeypatch.setattr("llm.judge_filing", boom)
    _add("a-1", FORFEITURE)
    rescore.run(verbose=False)


# ---------------------------------------------------------------------------
# --judge: judging history that was scored detector-only
# ---------------------------------------------------------------------------

JUDGMENT = {"score": 8, "verdict": "DEEP_LOOK", "direction": "BEARISH",
            "thesis": "CFO walks from unvested equity, no successor.", "disputed_signals": []}


def _fake_judge(monkeypatch, result=JUDGMENT):
    calls = []

    def judge_filing(payload, model=None):
        calls.append(payload)
        if isinstance(result, Exception):
            raise result
        return dict(result) if result else None

    monkeypatch.setattr("llm.judge_filing", judge_filing)
    monkeypatch.setattr("judge.judge_filing", judge_filing)
    return calls


def test_judge_ranks_an_unjudged_candidate(tmp_sqlite_db, monkeypatch):
    calls = _fake_judge(monkeypatch)
    filing_id = _add("a-1", FORFEITURE, verdict="MONITOR", score=6)
    stats = rescore.run(verbose=False, judge=True)

    row = database.get_filing_by_id(filing_id)
    assert len(calls) == 1
    assert stats["judge"]["judged"] == 1
    assert row["triage_verdict"] == "DEEP_LOOK"
    assert row["signal_score"] == 8
    assert row["top_signal"] == JUDGMENT["thesis"]
    assert json.loads(row["judge_json"])["verdict"] == "DEEP_LOOK"
    assert row["judged_at"] is not None


def test_judge_skips_judged_rows_and_non_candidates(tmp_sqlite_db, monkeypatch):
    calls = _fake_judge(monkeypatch)
    _add("a-1", FORFEITURE, judge=JUDGMENT, verdict="DEEP_LOOK", score=8)  # already judged
    _add("a-2", APPOINTMENT_ONLY, verdict="PASS", score=0)                  # no signal
    stats = rescore.run(verbose=False, judge=True)
    assert calls == []
    assert stats["judge"]["candidates"] == 0


def test_judge_respects_the_date_window(tmp_sqlite_db, monkeypatch):
    calls = _fake_judge(monkeypatch)
    _add("a-1", FORFEITURE)  # filed 2026-09-08
    rescore.run(verbose=False, judge=True, since="2026-09-09")
    assert calls == []
    rescore.run(verbose=False, judge=True, since="2026-09-01", until="2026-09-08")
    assert len(calls) == 1


def test_judge_dry_run_spends_nothing(tmp_sqlite_db, monkeypatch):
    calls = _fake_judge(monkeypatch)
    filing_id = _add("a-1", FORFEITURE, verdict="MONITOR", score=6)
    stats = rescore.run(verbose=False, judge=True, dry_run=True)
    assert calls == []
    assert stats["judge"]["candidates"] == 1
    assert database.get_filing_by_id(filing_id)["judge_json"] is None


def test_failed_judgment_leaves_the_detector_rank(tmp_sqlite_db, monkeypatch):
    _fake_judge(monkeypatch, result=None)
    filing_id = _add("a-1", FORFEITURE, verdict="MONITOR", score=6)
    stats = rescore.run(verbose=False, judge=True)
    row = database.get_filing_by_id(filing_id)
    assert stats["judge"]["failed"] == 1
    assert row["judge_json"] is None
    assert row["triage_verdict"] == "MONITOR"


def test_out_of_credits_stops_judging(tmp_sqlite_db, monkeypatch):
    from llm import OutOfCredits
    calls = _fake_judge(monkeypatch, result=OutOfCredits("insufficient_quota"))
    _add("a-1", FORFEITURE)
    _add("a-2", FORFEITURE)
    stats = rescore.run(verbose=False, judge=True)
    assert len(calls) == 1
    assert stats["judge"]["stopped"]
    assert stats["judge"]["judged"] == 0


def test_parallel_judging(tmp_sqlite_db, monkeypatch):
    calls = _fake_judge(monkeypatch)
    for n in range(6):
        _add(f"a-{n}", FORFEITURE)
    stats = rescore.run(verbose=False, judge=True, workers=3)
    assert stats["judge"]["judged"] == 6
    assert len(calls) == 6
