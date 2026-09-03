"""Tests for the signal inbox, the review loop, and signed label links.

The inbox is a work queue, not an archive — so what's tested here is mostly
what it LEAVES OUT: routine filings, unranked legacy rows, and anything
outside the window. Those omissions are the feature.
"""
import json

import pytest

import database
import labels


@pytest.fixture
def client(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def _add(accession, company="Acme Corp", verdict="DEEP_LOOK", score=8,
         direction="BEARISH", filed_date=None, signal_types="FORFEITURE_EXIT",
         top_signal="CFO walks from $4.2M unvested.", signals=None):
    from datetime import datetime
    database.insert_filing({
        "accession_no": accession, "company": company, "ticker": "ACME",
        "cik": "0001234567",
        "filed_date": filed_date or datetime.now().strftime("%Y-%m-%d"),
        "item_codes": "5.02", "summary": top_signal,
        "auto_category": "Management Change",
        "filing_url": "https://sec.gov/i.htm", "raw_text": "text",
        "triage_verdict": verdict, "signal_score": score,
        "signal_direction": direction, "top_signal": top_signal,
        "signal_types": signal_types,
        "signals_json": json.dumps(signals if signals is not None else [{
            "type": "FORFEITURE_EXIT", "direction": "BEARISH", "severity": 5,
            "evidence": "Jane Doe forfeits unvested compensation", "data": {},
        }]),
    })
    return database.get_filing_by_accession(accession)["id"]


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

def test_inbox_shows_a_flagged_filing_with_its_reason(client):
    _add("a-1")
    body = client.get("/").get_data(as_text=True)
    assert "Acme Corp" in body
    assert "CFO walks from $4.2M unvested." in body
    assert "FORFEITURE EXIT" in body  # the chip names the signal


def test_inbox_hides_routine_filings_by_default(client):
    _add("a-1", company="RealSignal")
    _add("a-2", company="RoutineCo", verdict="PASS", score=1,
         direction="NEUTRAL", signal_types=None, top_signal=None, signals=[])
    body = client.get("/").get_data(as_text=True)
    assert "RealSignal" in body
    assert "RoutineCo" not in body


def test_routine_filings_remain_reachable(client):
    """Hidden is not deleted — the user explicitly asked that nothing be
    thrown away."""
    _add("a-2", company="RoutineCo", verdict="PASS", score=1,
         direction="NEUTRAL", signal_types=None, top_signal=None, signals=[])
    assert "RoutineCo" in client.get("/?include_pass=1").get_data(as_text=True)
    assert "RoutineCo" in client.get("/all").get_data(as_text=True)


def test_inbox_ranks_by_score_not_by_date(client):
    _add("a-1", company="WeakSignal", score=4, filed_date="2026-09-02")
    _add("a-2", company="StrongSignal", score=9, filed_date="2026-08-28")
    body = client.get("/?days=365").get_data(as_text=True)
    assert body.index("StrongSignal") < body.index("WeakSignal")


def test_legacy_unrated_rows_are_not_ranked_into_the_inbox(client):
    """Rows analyzed before the rebuild have no signals. Ranking them would
    put filings on screen with no stated reason for being there."""
    _add("a-1", company="Rated")
    _add("a-2", company="LegacyRow", verdict=None, score=None,
         direction=None, signal_types=None, top_signal=None, signals=[])
    body = client.get("/").get_data(as_text=True)
    assert "Rated" in body
    assert "LegacyRow" not in body


def test_window_filter_excludes_older_filings(client):
    from datetime import datetime, timedelta
    older = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    _add("a-1", company="Zebracorp")
    _add("a-2", company="Yakworks", filed_date=older)

    body = client.get("/?days=7").get_data(as_text=True)
    assert "Zebracorp" in body
    assert "Yakworks" not in body
    assert "Yakworks" in client.get("/?days=90").get_data(as_text=True)


def test_direction_and_score_filters(client):
    _add("a-1", company="BearCo", direction="BEARISH", score=8)
    _add("a-2", company="BullCo", direction="BULLISH", score=5,
         signal_types="HURDLE_CONVICTION")
    bear = client.get("/?direction=BEARISH").get_data(as_text=True)
    assert "BearCo" in bear and "BullCo" not in bear
    high = client.get("/?min_score=7").get_data(as_text=True)
    assert "BearCo" in high and "BullCo" not in high


def test_signal_type_filter(client):
    # Distinctive names: the signal-type dropdown renders "Forfeiture Exit",
    # so a company called "Forfeit" would match the page for the wrong reason.
    _add("a-1", company="Zebracorp", signal_types="FORFEITURE_EXIT")
    _add("a-2", company="Yakworks", signal_types="HURDLE_CONVICTION")
    body = client.get("/?signal_type=HURDLE_CONVICTION").get_data(as_text=True)
    assert "Yakworks" in body and "Zebracorp" not in body


def test_garbage_query_params_do_not_500(client):
    _add("a-1")
    for qs in ("?days=abc", "?min_score=zzz", "?page=-4", "?direction=SIDEWAYS",
               "?days=99999", "?page=99999"):
        assert client.get("/" + qs).status_code == 200


def test_empty_inbox_explains_itself(client):
    """A blank screen reads as a broken tool; a quiet one has to say why."""
    body = client.get("/").get_data(as_text=True)
    assert "Nothing flagged" in body
    assert "routine filings are analyzed and hidden" in body


def test_corrupt_signals_json_does_not_break_the_page(client):
    filing_id = _add("a-1")
    database.update_filing_fields(filing_id, signals_json="{not json")
    assert client.get("/").status_code == 200


# ---------------------------------------------------------------------------
# Review loop
# ---------------------------------------------------------------------------

def test_review_serves_the_strongest_unlabelled_filing(client):
    _add("a-1", company="Weak", score=4)
    _add("a-2", company="Strong", score=9)
    body = client.get("/review").get_data(as_text=True)
    assert "Strong" in body
    assert "Weak" not in body


def test_labelled_filings_leave_the_review_queue(client):
    filing_id = _add("a-1", company="Judged")
    database.upsert_judgment(filing_id, "noise")
    body = client.get("/review").get_data(as_text=True)
    assert "Nothing left to review" in body


def test_api_label_records_and_reports_remaining(client):
    filing_id = _add("a-1")
    resp = client.post("/api/label", json={"filing_id": filing_id, "label": "signal",
                                           "note": "real forfeiture"})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert database.get_judgment(filing_id)["note"] == "real forfeiture"


def test_api_label_rejects_a_bad_label(client):
    filing_id = _add("a-1")
    resp = client.post("/api/label", json={"filing_id": filing_id, "label": "amazing"})
    assert resp.status_code == 400
    assert database.get_judgment(filing_id) is None


def test_api_label_rejects_a_bad_filing_id(client):
    assert client.post("/api/label", json={"filing_id": "abc", "label": "signal"}).status_code == 400


def test_api_label_undo(client):
    filing_id = _add("a-1")
    client.post("/api/label", json={"filing_id": filing_id, "label": "noise"})
    client.post("/api/label", json={"filing_id": filing_id, "label": "undo"})
    assert database.get_judgment(filing_id) is None


def test_guidelines_can_be_added_and_removed(client):
    client.post("/guidelines", data={"rule": "Ignore SPAC director shuffles"})
    rules = database.get_guidelines()
    assert rules[0]["rule"] == "Ignore SPAC director shuffles"

    client.post(f"/guidelines/{rules[0]['id']}/remove")
    assert database.get_guidelines() == []


def test_blank_guideline_is_ignored(client):
    client.post("/guidelines", data={"rule": "   "})
    assert database.get_guidelines() == []


def test_watchlist_seeding_route(client):
    filing_id = _add("a-1")
    database.add_to_watchlist(filing_id)
    client.post("/seed-labels")
    assert database.get_judgment(filing_id)["label"] == "signal"


# ---------------------------------------------------------------------------
# Signed label links — the email path
# ---------------------------------------------------------------------------

@pytest.fixture
def signing_secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-real-secret-for-tests")


def test_token_roundtrip(signing_secret):
    token = labels.make_token(42, "signal")
    assert labels.read_token(token) == (42, "signal")


def test_tampered_token_is_rejected(signing_secret):
    token = labels.make_token(42, "signal")
    assert labels.read_token(token[:-3] + "aaa") == (None, None)


def test_expired_token_is_rejected(signing_secret):
    token = labels.make_token(42, "signal")
    assert labels.read_token(token, max_age=-1) == (None, None)


def test_signing_refuses_the_public_default_secret(monkeypatch):
    """The fallback key is committed to a public repository. Signing with it
    would let anyone who reads the repo forge labels — and, since the same key
    signs Flask sessions, forge a login too."""
    monkeypatch.setenv("SECRET_KEY", "8k-analyzer-secret-key")
    assert labels.signing_available() is False
    with pytest.raises(labels.InsecureSecretError):
        labels.make_token(1, "signal")


def test_signing_refuses_an_unset_secret(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    assert labels.signing_available() is False


def test_label_link_confirms_before_writing(client, signing_secret, monkeypatch):
    """GET must not write. Corporate mail scanners (SafeLinks, Gmail prefetch)
    request every URL in an email before the recipient sees it, and a digest
    row carries BOTH a Signal and a Noise link — so a silent write would let a
    scanner label the whole digest, last link winning."""
    monkeypatch.setenv("TRIAL_CODE", "SOME-CODE")
    filing_id = _add("a-1")

    resp = client.get(f"/label/{labels.make_token(filing_id, 'noise')}")

    assert resp.status_code == 200
    assert "Mark this as noise?" in resp.get_data(as_text=True)
    assert database.get_judgment(filing_id) is None, "GET wrote a label"


def test_a_scanner_fetching_both_links_labels_nothing(client, signing_secret, monkeypatch):
    """The exact sequence an email scanner performs."""
    monkeypatch.setenv("TRIAL_CODE", "SOME-CODE")
    filing_id = _add("a-1")

    client.get(f"/label/{labels.make_token(filing_id, 'signal')}")
    client.get(f"/label/{labels.make_token(filing_id, 'noise')}")

    assert database.get_judgment(filing_id) is None


def test_confirming_records_the_label_without_a_login(client, signing_secret, monkeypatch):
    """The point of the email path survives: one tap in the mail client, one
    tap on the page, no session required."""
    monkeypatch.setenv("TRIAL_CODE", "SOME-CODE")
    filing_id = _add("a-1")

    resp = client.post(f"/label/{labels.make_token(filing_id, 'noise')}")

    assert resp.status_code == 200
    assert "Marked as noise" in resp.get_data(as_text=True)
    assert database.get_judgment(filing_id)["label"] == "noise"
    assert database.get_judgment(filing_id)["source"] == "digest_link"


def test_confirm_page_warns_before_replacing_a_deliberate_label(client, signing_secret):
    """A 30-day token re-opened after a considered /review label would
    otherwise overwrite it — and wipe the note with it."""
    filing_id = _add("a-1")
    database.upsert_judgment(filing_id, "signal", note="real forfeiture",
                             source="review_ui")

    body = client.get(f"/label/{labels.make_token(filing_id, 'noise')}").get_data(as_text=True)
    assert "Currently labelled" in body
    assert "real forfeiture" in body
    assert database.get_judgment(filing_id)["label"] == "signal"


def test_other_pages_still_require_the_trial_code(client, monkeypatch):
    """Exempting the label route must not open the rest of the app."""
    monkeypatch.setenv("TRIAL_CODE", "SOME-CODE")
    assert client.get("/", follow_redirects=False).status_code == 302


def test_label_link_undo(client, signing_secret):
    filing_id = _add("a-1")
    token = labels.make_token(filing_id, "noise")
    client.post(f"/label/{token}")
    resp = client.post(f"/label/{token}", data={"action": "undo"})
    assert "Undone" in resp.get_data(as_text=True)
    assert database.get_judgment(filing_id) is None


def test_invalid_label_link_shows_a_friendly_page(client, signing_secret):
    resp = client.get("/label/not-a-real-token")
    assert resp.status_code == 400
    assert "didn't work" in resp.get_data(as_text=True)


def test_label_link_for_a_deleted_filing(client, signing_secret):
    resp = client.get(f"/label/{labels.make_token(9999, 'signal')}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Signal type counts
# ---------------------------------------------------------------------------

def test_signal_type_counts_aggregate_across_filings(tmp_sqlite_db):
    _add("a-1", signal_types="FORFEITURE_EXIT,NO_SUCCESSOR")
    _add("a-2", signal_types="FORFEITURE_EXIT")
    counts = database.get_signal_type_counts(days=30)
    assert counts["FORFEITURE_EXIT"] == 2
    assert counts["NO_SUCCESSOR"] == 1


# ---------------------------------------------------------------------------
# Header timestamp — the platform trap
# ---------------------------------------------------------------------------

def test_inbox_renders_the_last_updated_timestamp(client):
    """This 500'd in real use and passed every test, because the tests never
    recorded a backfill so the branch never ran. The template used '%-I' to
    drop the hour's leading zero — a Linux-only strftime directive that raises
    ValueError on Windows, surfacing as a 500 on the page."""
    _add("a-1")
    database.update_last_backfill("scheduled")

    resp = client.get("/")
    assert resp.status_code == 200
    assert "updated" in resp.get_data(as_text=True)


def test_timestamp_filter_is_platform_safe():
    from datetime import datetime
    from app import format_timestamp

    assert format_timestamp(datetime(2026, 9, 2, 7, 30)) == "Sep 02, 7:30 AM"
    assert format_timestamp(datetime(2026, 9, 2, 19, 5)) == "Sep 02, 7:05 PM"
    assert format_timestamp(datetime(2026, 9, 2, 11, 45)) == "Sep 02, 11:45 AM"
    assert format_timestamp(None) == ""
    assert format_timestamp("not a datetime") == "not a datetime"


def test_window_control_reflects_a_custom_value(client):
    """A hand-typed ?days= that isn't a preset used to leave the select
    showing "Today" while the page listed a much longer window — the control
    contradicting the header right next to it."""
    _add("a-1")
    body = client.get("/?days=45").get_data(as_text=True)
    assert '<option value="45" selected>45 days</option>' in body


def test_out_of_range_window_is_clamped_and_shown_honestly(client):
    """days=400 clamps to the 365 maximum, and the control must show 365 —
    not fall back to the first option."""
    _add("a-1")
    body = client.get("/?days=400").get_data(as_text=True)
    assert '<option value="365" selected>1 year</option>' in body


def test_preset_windows_still_select_correctly(client):
    _add("a-1")
    body = client.get("/?days=30").get_data(as_text=True)
    assert '<option value="30" selected>30 days</option>' in body
