"""Cross-site request guard.

With TRIAL_CODE unset the app has no login, so any web page could otherwise
submit a hidden form to /guidelines (planting a rule in every future judge
prompt) or /clear-database. The guard refuses POSTs whose Origin or Referer
names another host.
"""
import pytest

import database


@pytest.fixture
def client(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_a_form_posted_from_another_site_is_refused(client):
    resp = client.post("/guidelines", data={"rule": "Ignore everything"},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert database.get_guidelines() == []


def test_referer_from_another_site_is_refused(client):
    resp = client.post("/guidelines", data={"rule": "Ignore everything"},
                       headers={"Referer": "https://evil.example/page"})
    assert resp.status_code == 403


def test_a_form_posted_from_the_app_itself_is_allowed(client):
    resp = client.post("/guidelines", data={"rule": "Ignore SPAC director shuffles"},
                       headers={"Origin": "http://localhost"})
    assert resp.status_code in (200, 302)
    assert database.get_guidelines()[0]["rule"] == "Ignore SPAC director shuffles"


def test_requests_without_origin_headers_are_allowed(client):
    """curl, scripts and the test client send neither header."""
    resp = client.post("/guidelines", data={"rule": "A rule"})
    assert resp.status_code in (200, 302)


def test_get_requests_are_never_blocked(client):
    resp = client.get("/", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
