"""bakeoff.py — read-only model comparison. Pins the sampling and the
cost arithmetic; the model calls themselves are exercised in production."""
import json

import pytest

import bakeoff
import database


def test_flex_halves_the_measured_cost():
    standard = bakeoff.cost("gpt-6-luna", 10_000, 1_000, "default")
    assert standard == pytest.approx((10_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert bakeoff.cost("gpt-6-luna", 10_000, 1_000, "flex") == pytest.approx(standard / 2)


def test_sample_takes_judged_and_unjudged_scored_8ks_only(tmp_sqlite_db):
    for i in range(6):
        database.insert_filing({
            "accession_no": f"b-{i}", "company": f"Co {i}", "ticker": "AAA", "cik": "1",
            "filed_date": "2026-09-01", "item_codes": "5.02", "filing_url": "u",
            "raw_text": "text", "pipeline_version": "v4.2-signals",
            "context_json": json.dumps({"price": 10}),
            "judge_json": json.dumps({"verdict": "MONITOR"}) if i < 3 else None,
        })
    database.insert_filing({   # never scored: excluded
        "accession_no": "b-x", "company": "Old", "ticker": "AAA", "cik": "1",
        "filed_date": "2026-03-01", "item_codes": "5.02", "filing_url": "u", "raw_text": "t",
    })
    rows = bakeoff.sample(4)
    assert len(rows) == 4
    assert sum(1 for r in rows if r.get("judge_json")) == 2
    assert all(r["accession_no"] != "b-x" for r in rows)
