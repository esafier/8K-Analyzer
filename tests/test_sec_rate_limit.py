"""Tests for SEC 429 handling — the failure that silently ate filings.

A 429 from EDGAR means the IP is in a ~10-minute penalty box. The old retry
ladder (1s, 2s, 4s) gave up after ~8 seconds, so once the block landed every
remaining fetch in a backfill burned four doomed attempts and returned empty
text. These tests pin the behavior that replaced it:

  * 429 backoffs are minutes long, not seconds
  * the block is published process-wide, so other callers wait it out
    instead of piling on more 429s
  * SEC's Retry-After header still wins when it sends one
  * transient 5xx keeps the old short exponential ladder
"""
import time

import pytest
import requests
from unittest.mock import Mock, patch

import fetcher


@pytest.fixture(autouse=True)
def clean_throttle(monkeypatch):
    """Reset the module-level gate between tests and make sleeps instant.

    Real waits are minutes long by design — the tests record what would have
    been slept instead of actually sleeping, and drive the clock by hand.
    """
    fetcher._reset_sec_throttle()
    slept = []

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: fake_now["t"])

    def fake_sleep(seconds):
        slept.append(seconds)
        fake_now["t"] += seconds

    monkeypatch.setattr(fetcher.time, "sleep", fake_sleep)
    yield slept
    fetcher._reset_sec_throttle()


def _rate_limited(retry_after=None):
    """A response whose raise_for_status() raises a 429 HTTPError."""
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    err = requests.exceptions.HTTPError("429 Client Error")
    err.response = Mock(status_code=429, headers=headers)
    resp = Mock(status_code=429, text="", headers=headers)
    resp.raise_for_status = Mock(side_effect=err)
    return resp


def _server_error(status=503):
    err = requests.exceptions.HTTPError(f"{status} Server Error")
    err.response = Mock(status_code=status, headers={})
    resp = Mock(status_code=status, text="", headers={})
    resp.raise_for_status = Mock(side_effect=err)
    return resp


def _ok(text="fine"):
    resp = Mock(status_code=200, text=text, headers={})
    resp.raise_for_status = Mock()
    return resp


# ---------------------------------------------------------------------------
# Backoff sizing
# ---------------------------------------------------------------------------

def test_429_backoff_outlasts_a_ten_minute_block(clean_throttle):
    """Total wait across the retry ladder must exceed SEC's ~10-minute block.

    This is the whole point: the old ladder totalled ~8s against a 600s block,
    so failure was guaranteed. Anything under 600s here reintroduces the bug.
    """
    assert sum(fetcher.SEC_RATE_LIMIT_BACKOFF) > 600


def test_429_retries_then_succeeds_without_losing_the_filing(clean_throttle):
    """Two 429s then a 200 — the request survives instead of returning empty."""
    responses = [_rate_limited(), _rate_limited(), _ok("recovered")]
    with patch("fetcher.requests.get", side_effect=responses):
        resp = fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)

    assert resp.text == "recovered"
    # Slept the first two rungs of the ladder, in order.
    assert clean_throttle[:2] == [
        fetcher.SEC_RATE_LIMIT_BACKOFF[0],
        fetcher.SEC_RATE_LIMIT_BACKOFF[1],
    ]


def test_429_raises_after_exhausting_retries(clean_throttle):
    """Persistent 429 still raises so callers can record a real failure."""
    with patch("fetcher.requests.get", side_effect=[_rate_limited()] * 10):
        with pytest.raises(requests.exceptions.HTTPError):
            fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)


def test_retry_after_header_overrides_the_default_ladder(clean_throttle):
    """SEC knows how long its own block is — believe it when it says."""
    with patch("fetcher.requests.get", side_effect=[_rate_limited(retry_after=42), _ok()]):
        fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)

    assert 42 in clean_throttle


def test_retry_after_is_capped(clean_throttle):
    """An absurd Retry-After must not hang a backfill for an hour."""
    with patch("fetcher.requests.get", side_effect=[_rate_limited(retry_after=99999), _ok()]):
        fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)

    assert max(clean_throttle) <= fetcher.SEC_MAX_COOLDOWN


# ---------------------------------------------------------------------------
# The process-wide gate
# ---------------------------------------------------------------------------

def test_rate_limit_block_is_shared_across_callers(clean_throttle):
    """A 429 on one filing parks every other SEC caller.

    Previously each filing discovered the block independently and burned four
    attempts finding out. Now the first one records it and the rest wait.
    """
    # Filing A hits the block and gives up.
    with patch("fetcher.requests.get", side_effect=[_rate_limited()] * 10):
        with pytest.raises(requests.exceptions.HTTPError):
            fetcher._sec_get_with_retry("https://sec.gov/a", fetcher.FILING_HEADERS)

    clean_throttle.clear()

    # Filing B is a completely separate call — it should wait out the
    # remaining cooldown before issuing its first request.
    with patch("fetcher.requests.get", side_effect=[_ok("b")]) as mock_get:
        fetcher._sec_get_with_retry("https://sec.gov/b", fetcher.FILING_HEADERS)

    assert mock_get.call_count == 1
    assert sum(clean_throttle) > 0, "second caller ignored the active block"


def test_gate_clears_once_the_cooldown_expires(clean_throttle):
    """After the block lapses, requests flow again with only pacer delay."""
    fetcher._note_sec_rate_limit(120)
    with patch("fetcher.requests.get", side_effect=[_ok()]):
        fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)

    clean_throttle.clear()

    # Cooldown is spent now; a fresh call should not wait on it again.
    with patch("fetcher.requests.get", side_effect=[_ok()]):
        fetcher._sec_get_with_retry("https://sec.gov/y", fetcher.FILING_HEADERS)

    assert sum(clean_throttle) <= fetcher.REQUEST_DELAY


def test_note_rate_limit_extends_never_shortens(clean_throttle):
    """A short 429 arriving mid-block must not let everyone back in early."""
    fetcher._note_sec_rate_limit(600)
    before = fetcher._sec_cooldown_until
    fetcher._note_sec_rate_limit(5)
    assert fetcher._sec_cooldown_until == before


# ---------------------------------------------------------------------------
# Non-429 behavior is unchanged
# ---------------------------------------------------------------------------

def test_transient_5xx_keeps_short_exponential_backoff(clean_throttle):
    """503s are one-off blips, not penalty boxes — don't wait minutes on them."""
    with patch("fetcher.requests.get", side_effect=[_server_error(503), _ok()]):
        fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)

    assert clean_throttle, "expected a backoff sleep"
    assert max(clean_throttle) < fetcher.SEC_RATE_LIMIT_BACKOFF[0]


def test_5xx_does_not_park_other_callers(clean_throttle):
    """Only 429 means "the IP is blocked" — a 503 must not gate everyone."""
    with patch("fetcher.requests.get", side_effect=[_server_error(500), _ok()]):
        fetcher._sec_get_with_retry("https://sec.gov/x", fetcher.FILING_HEADERS)

    assert fetcher._sec_cooldown_until <= time.monotonic() or fetcher._sec_cooldown_until == 0.0


def test_permanent_4xx_fails_immediately(clean_throttle):
    """404 is not retryable — fail fast instead of sleeping through the ladder."""
    err = requests.exceptions.HTTPError("404 Not Found")
    err.response = Mock(status_code=404, headers={})
    resp = Mock(status_code=404, text="", headers={})
    resp.raise_for_status = Mock(side_effect=err)

    with patch("fetcher.requests.get", side_effect=[resp]) as mock_get:
        with pytest.raises(requests.exceptions.HTTPError):
            fetcher._sec_get_with_retry("https://sec.gov/missing", fetcher.FILING_HEADERS)

    assert mock_get.call_count == 1
    assert clean_throttle == [] or sum(clean_throttle) <= fetcher.REQUEST_DELAY
